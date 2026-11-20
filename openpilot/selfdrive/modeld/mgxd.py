"""Standalone MIGraphX inference daemon for the big nav model.

Own process, own HIP context. The client passes the VisionIPC camera buffer fds
(shared memory) over SCM_RIGHTS; this process does the OpenCV warp and runs the
graph, so modeld only pays for a small control message and the 74 KB output.

Protocol v3 (little endian):
  hello : b'MGX3' + <4i> (w, h, stride, uv_offset) + <1i> buffer_len
  frame : ancillary 2 fds (img, big) + payload:
            <B> flags + <18f> transforms (img 9, big 9) + <8f> desire + <2f> tc + <2f> action
            + [nav_map 65536f if flags&1] + [nav 47f if flags&2]
            flags&8 clears map history; flags&16 clears navigation history before inference
  reply : b'MGX3' + outputs 18580f
          flags&4: b'MGX4' + fixed navigation receipt + outputs 18580f
          flags&24: b'MGX5' + reset acknowledgment + navigation receipt + outputs 18580f
"""
import mmap
import os
import socket
import struct
import time
import numpy as np

from openpilot.selfdrive.modeld.nav_evidence_protocol import (
  ERROR_FEED, ERROR_GPU, ERROR_OUTPUT, ERROR_RECEIPT, ERROR_STAGING, RECEIPT_MAGIC,
  REQUEST_NAV_SESSION, REQUEST_RECEIPT, REQUEST_RESET_MAP, REQUEST_RESET_NAVIGATION, RESET_FLAGS, RESET_RECEIPT_MAGIC,
  crc32_buffer, encode_receipt, encode_reset_ack,
)

MODEL = os.getenv("MODELD_MIGRAPHX_MXR", os.path.expanduser("~/fwtest/nav.mxr"))
ONNX = os.getenv("MODELD_MIGRAPHX_ONNX", os.path.expanduser("~/fwtest/nav_v2_mgx.onnx"))
SOCK = os.getenv("MODELD_MGXD_SOCK", "/tmp/mgxd.sock")

MAGIC = b"MGX3"
IMG_SHAPE = (2, 6, 128, 256)
NAV_MAP_SHAPE = (1, 1, 256, 256)
INVERT = os.getenv("MODELD_MIGRAPHX_INVERT", "1") == "1"
UV_MODE = os.getenv("MODELD_MIGRAPHX_UV", "col2")
SHIFT = tuple(float(v) for v in os.getenv("MODELD_MIGRAPHX_SHIFT", "0.5,0.0").split(","))
GPU_EVIDENCE_INTERVAL_NS = 1_000_000_000


def prepare_navigation_evidence(feed):
  """Capture the worker's exact host feed before inference without affecting it."""
  started = time.monotonic_ns()
  receipt = {'error_code': 0, 'feed_valid': False}
  try:
    receipt['feed_nav_crc32'] = crc32_buffer(feed['navigation'])
    receipt['feed_map_crc32'] = crc32_buffer(feed['nav_map'])
    receipt['feed_valid'] = True
  except Exception:
    receipt.update(diagnostic_error=True, error_code=ERROR_FEED)
  receipt['diagnostic_duration_ns'] = time.monotonic_ns() - started
  return receipt


def finish_navigation_evidence(receipt, engine, output, *, worker_session, step_seq,
                               started_ns, completed_ns, sample_gpu):
  """Report completed work; failed diagnostics never invalidate the inference result."""
  started = time.monotonic_ns()
  result = dict(receipt)
  result.update(worker_session=worker_session, step_seq=step_seq, completed_mono_time=completed_ns,
                worker_step_ns=max(completed_ns - started_ns, 0), capture=bool(engine is not None and engine.capture),
                navigation_reset_supported=engine is None or callable(getattr(engine, 'reset_navigation', None)),
                synchronized=engine is not None, gpu_sampled=False, gpu_match=False, gpu_error=False,
                gpu_sample_mono_time=0, gpu_sample_duration_ns=0, gpu_nav_crc32=0, gpu_map_crc32=0)
  if engine is not None:
    try:
      evidence = engine.navigation_evidence(sample_gpu=sample_gpu)
      result.update({key: value for key, value in evidence.items() if key not in ('error_code', 'diagnostic_error')})
      result['error_code'] |= evidence.get('error_code', 0)
      result['diagnostic_error'] = bool(result.get('diagnostic_error') or evidence.get('diagnostic_error'))
    except Exception:
      result.update(staging_valid=False, diagnostic_error=True, gpu_error=sample_gpu)
      result['error_code'] |= ERROR_STAGING | (ERROR_GPU if sample_gpu else 0)
  try:
    result['output_crc32'] = crc32_buffer(output)
    result['output_valid'] = True
  except Exception:
    result.update(output_valid=False, diagnostic_error=True)
    result['error_code'] |= ERROR_OUTPUT
  result['gpu_match'] = bool(result.get('gpu_sampled') and result.get('feed_valid') and result.get('staging_valid')
                             and result['feed_nav_crc32'] == result['staging_nav_crc32'] == result['gpu_nav_crc32']
                             and result['feed_map_crc32'] == result['staging_map_crc32'] == result['gpu_map_crc32'])
  result['diagnostic_duration_ns'] += time.monotonic_ns() - started
  return result


def warp_camera(cv2, raw, w, h, stride, uv_off, transform):
  y = np.frombuffer(raw, np.uint8, count=stride * h).reshape(h, stride)[:, :w]
  uv = np.frombuffer(raw, np.uint8, offset=uv_off, count=stride * (h // 2)).reshape(h // 2, stride)[:, :w]
  if INVERT:
    try:
      M = np.linalg.inv(transform)
    except np.linalg.LinAlgError:
      M = np.eye(3, dtype=np.float32)
  else:
    M = transform
  if SHIFT != (0.0, 0.0):
    dx, dy = SHIFT
    M = M.copy()
    M[:, 2] -= M[:, :2] @ np.array([dx, dy], np.float32)
  yw = cv2.warpPerspective(y, M, (512, 256), flags=cv2.INTER_LINEAR)
  if UV_MODE == "elem":
    E = np.array([[1.0, 1.0, 0.5], [1.0, 1.0, 0.5], [2.0, 2.0, 1.0]], np.float32)
    M_uv = np.linalg.inv(transform * E) if INVERT else (transform * E)
  elif UV_MODE == "same":
    M_uv = M
  else:
    M_uv = M.copy()
    M_uv[:, 2] *= 0.5
  uvw = cv2.warpPerspective(uv.reshape(h // 2, w // 2, 2), M_uv, (256, 128), flags=cv2.INTER_LINEAR)
  result = np.empty((6, 128, 256), dtype=np.uint8)
  result[0] = yw[0::2, 0::2]
  result[1] = yw[1::2, 0::2]
  result[2] = yw[0::2, 1::2]
  result[3] = yw[1::2, 1::2]
  result[4] = uvw[:, :, 0]
  result[5] = uvw[:, :, 1]
  return result


def recv_exact(conn, n):
  buf = bytearray(n)
  view = memoryview(buf)
  got = 0
  while got < n:
    k = conn.recv_into(view[got:], n - got)
    if k == 0:
      return None
    got += k
  return buf


def create_engine(migraphx):
  mode = os.environ.get("MODELD_MGXD_ENGINE", "legacy")
  if mode == "capture":
    from openpilot.selfdrive.modeld.mgxd_engine import ResidentEngine
    return ResidentEngine(migraphx, MODEL)
  if mode != "legacy":
    raise ValueError(f"Unknown MODELD_MGXD_ENGINE: {mode}")
  return None


def recv_frame(conn):
  fds = array("i")
  try:
    msg, ancillary, flags, _ = conn.recvmsg(121, socket.CMSG_SPACE(8))
    for level, kind, data in ancillary:
      if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
        fds.frombytes(data[:len(data) - len(data) % fds.itemsize])
    if not msg or len(fds) != 2 or flags & getattr(socket, 'MSG_CTRUNC', 8):
      return None, []
    rest = recv_exact(conn, 121 - len(msg))
    if rest is None:
      return None, []
    result, fds = fds, array("i")
    return msg + rest, result
  finally:
    for fd in fds:
      os.close(fd)


def serve():
  import cv2
  import migraphx

  cpu = os.getenv("MODELD_MGXD_CPU")
  if cpu:
    try:
      os.sched_setaffinity(0, {int(cpu)})
      print(f"mgxd: affinity cpu {cpu}", flush=True)
    except OSError as e:
      print(f"mgxd: affinity failed: {e}", flush=True)

  from openpilot.selfdrive.modeld.mgxd_runtime import audit_runtime
  audit_runtime(migraphx)
  engine = create_engine(migraphx)
  try:
    _serve(cv2, migraphx, engine)
  finally:
    if engine is not None:
      engine.close()


def _serve(cv2, migraphx, engine):
  def load_prog():
    if os.path.exists(MODEL):
      p = migraphx.load(MODEL)
    else:
      p = migraphx.parse_onnx(ONNX)
      p.compile(migraphx.get_target("gpu"), offload_copy=True)
    print(f"mgxd: program ready ({MODEL})", flush=True)
    return p

  prog = load_prog() if engine is None else None
  from openpilot.selfdrive.modeld.mgxd_runtime import audit_runtime
  import json
  print("mgxd: runtime " + json.dumps(audit_runtime(migraphx)), flush=True)
  if engine is not None:
    metadata = engine.metadata()
    metadata['graph_audit'] = {k: v for k, v in metadata['graph_audit'].items() if k != 'topology'}
    print("mgxd: capture ready " + json.dumps(metadata), flush=True)

  state = {} if engine is not None else {
    "state_img_q": np.zeros((2, 5, 6, 128, 256), np.uint8),
    "state_desire_q": np.zeros((132, 1, 8), np.float32),
    "state_feat_q": np.zeros((128, 1, 16384), np.float32),
    "state_map_features_q": np.zeros((128, 1, 64), np.float32),
    "state_navigation_features_q": np.zeros((128, 1, 64), np.float32),
  }
  cached_map = np.zeros(NAV_MAP_SHAPE, np.float32)
  cached_nav = np.zeros((1, 47), np.float32)
  worker_session = (time.monotonic_ns() ^ (os.getpid() << 32)) & 0xffffffffffffffff
  step_seq = 0
  next_gpu_sample_ns = 0
  prof = {"warp": [], "run": [], "send": []} if os.getenv("MGXD_PROFILE") == "1" else None

  if os.path.exists(SOCK):
    os.unlink(SOCK)
  srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  srv.bind(SOCK)
  os.chmod(SOCK, 0o666)
  srv.listen(1)
  print("mgxd: listening", flush=True)

  while True:
    conn, _ = srv.accept()
    gpu_fault = None
    try:
      hello = recv_exact(conn, 4 + 16 + 4)
      if hello is None:
        continue
      w, h, stride, uv_off, buflen = struct.unpack("<4ii", hello[4:])
      while True:
        msg, fds = recv_frame(conn)
        if msg is None:
          break
        maps = []
        try:
          flags = msg[0]
          fixed = struct.unpack_from("<30f", msg, 1)
          extra = recv_exact(conn, (65536 * 4 if flags & 1 else 0) + (47 * 4 if flags & 2 else 0))
          if extra is None:
            break
          eoff = 0
          if flags & 1:
            cached_map = np.frombuffer(extra[eoff:eoff + 65536 * 4], np.float32).reshape(NAV_MAP_SHAPE).copy()
            eoff += 65536 * 4
          if flags & 2:
            cached_nav = np.frombuffer(extra[eoff:eoff + 47 * 4], np.float32).reshape(1, 47).copy()
            eoff += 47 * 4
          transforms = np.array(fixed[:18], np.float32).reshape(2, 3, 3)
          desire = np.array(fixed[18:26], np.float32)
          tc = np.array(fixed[26:28], np.float32)
          action = np.array(fixed[28:30], np.float32)
          _t0 = time.perf_counter()
          for fd in fds[:2]:
            maps.append(mmap.mmap(fd, buflen, prot=mmap.PROT_READ))
          img = warp_camera(cv2, maps[0], w, h, stride, uv_off, transforms[0])
          big = warp_camera(cv2, maps[1], w, h, stride, uv_off, transforms[1])
          new_img = np.stack([img, big])
          _t1 = time.perf_counter()
          reset_flags = flags & RESET_FLAGS
          if flags & REQUEST_NAV_SESSION and (engine is None or callable(getattr(engine, 'reset_navigation', None))):
            reset_flags |= RESET_FLAGS
          if reset_flags:
            if engine is not None:
              try:
                engine.reset_navigation(map_reset=bool(reset_flags & REQUEST_RESET_MAP),
                                        navigation_reset=bool(reset_flags & REQUEST_RESET_NAVIGATION))
              except Exception as error:
                raise RuntimeError('HIP navigation history reset failed; no inference retry') from error
            else:
              if reset_flags & REQUEST_RESET_MAP:
                state['state_map_features_q'].fill(0)
              if reset_flags & REQUEST_RESET_NAVIGATION:
                state['state_navigation_features_q'].fill(0)
          feed = dict(state)
          feed.update({"new_img": new_img, "desire": desire,
                       "traffic_convention": tc.reshape(1, 2), "action_t": action.reshape(1, 2),
                       "nav_map": cached_map, "navigation": cached_nav})
          receipt = None
          if flags & REQUEST_RECEIPT or reset_flags:
            try:
              receipt = prepare_navigation_evidence(feed)
            except Exception:
              receipt = {'error_code': ERROR_FEED, 'diagnostic_error': True, 'diagnostic_duration_ns': 0}
          step_started_ns = time.monotonic_ns()
          if engine is not None:
            try:
              out0 = engine.step(feed, audit=True)
            except Exception as error:
              raise RuntimeError("HIP resident inference failed; no fallback") from error
          else:
            out = prog.run({k: migraphx.argument(v) for k, v in feed.items()})
            vals = list(out.values()) if isinstance(out, dict) else list(out)
            out0 = np.asarray(vals[0], np.float32).reshape(-1)
            for name, val in zip(("state_img_q", "state_desire_q", "state_feat_q",
                                  "state_map_features_q", "state_navigation_features_q"), vals[1:],
                                 strict=False):
              arr = np.asarray(val)
              if arr.shape == state[name].shape:
                np.copyto(state[name], arr, casting="unsafe")
          completed_ns = time.monotonic_ns()
          step_seq += 1
          _t2 = time.perf_counter()
          output_bytes = out0.tobytes()
          if flags & REQUEST_RECEIPT or reset_flags:
            sample_gpu = engine is not None and completed_ns >= next_gpu_sample_ns
            if sample_gpu:
              next_gpu_sample_ns = completed_ns + GPU_EVIDENCE_INTERVAL_NS
            try:
              receipt = finish_navigation_evidence(receipt, engine, output_bytes, worker_session=worker_session,
                                                    step_seq=step_seq, started_ns=step_started_ns,
                                                    completed_ns=completed_ns, sample_gpu=sample_gpu)
              receipt_bytes = encode_receipt(receipt)
            except Exception:
              receipt_bytes = ERROR_RECEIPT
            prefix = (RESET_RECEIPT_MAGIC + encode_reset_ack(reset_flags, worker_session, step_seq)
                      if reset_flags else RECEIPT_MAGIC)
            conn.sendall(prefix + receipt_bytes + output_bytes)
          else:
            conn.sendall(MAGIC + output_bytes)
          if prof is not None:
            import json
            prof["warp"].append(_t1 - _t0)
            prof["run"].append(_t2 - _t1)
            prof["send"].append(time.perf_counter() - _t2)
            if len(prof["run"]) >= 100:
              o = {}
              for k, v in prof.items():
                a = np.array(v) * 1000.0
                o[k] = {"p50": round(float(np.percentile(a, 50)), 2), "p95": round(float(np.percentile(a, 95)), 2)}
              open("/tmp/mgxd_profile.json", "w").write(json.dumps(o))
              prof = {k: [] for k in prof}
        except Exception as e:
          import traceback
          traceback.print_exc()
          if any(k in str(e) for k in ("launch failure", "allocate_gpu", "HIP", "hip", "unspecified")):
            gpu_fault = e
            break
        finally:
          for m in maps:
            m.close()
          for fd in fds:
            os.close(fd)
    except (BrokenPipeError, ConnectionResetError):
      pass
    except Exception:
      import traceback
      traceback.print_exc()
    finally:
      conn.close()
    if gpu_fault is not None:
      # Recompiling or reloading cannot repair a damaged HIP context. Exit after
      # closing this connection so recovery uses a fresh, bounded process start.
      srv.close()
      raise RuntimeError("mgxd: GPU fault; exiting instead of reloading the damaged context") from gpu_fault


from array import array

if __name__ == "__main__":
  serve()
