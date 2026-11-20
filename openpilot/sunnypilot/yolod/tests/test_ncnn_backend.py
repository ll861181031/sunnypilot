"""ncnn + Vulkan backend tests (skipped when ncnn or the ncnn model is missing).

Creating a Vulkan net in the pytest process is avoided: ncnn crashed when an ncnn
CPU net and a Vulkan net were mixed in one process during development, and this
suite also creates ONNX Runtime sessions. Everything that touches the GPU runs in
``tests/ncnn_helper.py`` in a subprocess; the in-process tests only cover config,
names, error paths and the export tool.
"""

import json
import os
import subprocess
import sys

import numpy as np
import pytest

from openpilot.sunnypilot.yolod.detector import Detector
from openpilot.sunnypilot.yolod.paths import DEFAULT_NCNN_BIN, DEFAULT_NCNN_NAMES, DEFAULT_NCNN_PARAM, DEFAULT_ONNX_MODEL

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
NIGHT_ROUTE = os.path.expanduser("~/.comma/media/0/realdata/00000097--4f3f72a3b0--12")
NIGHT_VIDEO = os.path.join(NIGHT_ROUTE, "fcamera.hevc")
NCNN_MODELS_PRESENT = all(os.path.isfile(path) for path in (DEFAULT_NCNN_PARAM, DEFAULT_NCNN_BIN, DEFAULT_NCNN_NAMES))
MEDIA_PRESENT = os.path.isfile(NIGHT_VIDEO)

try:
  import ncnn  # noqa: F401 - only a presence probe
  HAS_NCNN = True
except ImportError:
  HAS_NCNN = False

requires_ncnn = pytest.mark.skipif(not HAS_NCNN, reason="ncnn is not installed in this environment")
requires_ncnn_model = pytest.mark.skipif(not NCNN_MODELS_PRESENT, reason="converted ncnn model files are missing")


def _tool(name: str):
  from importlib import import_module
  return import_module(f"openpilot.sunnypilot.yolod.tools.{name}")


def _iou(a, b) -> float:
  xx1 = max(a[0], b[0])
  yy1 = max(a[1], b[1])
  xx2 = min(a[2], b[2])
  yy2 = min(a[3], b[3])
  intersection = max(0.0, xx2 - xx1) * max(0.0, yy2 - yy1)
  area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
  area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
  union = area_a + area_b - intersection
  return intersection / union if union > 0 else 0.0


def _match_frame(reference, candidate):
  """Greedy same-class matching at IoU >= 0.5. Returns (matched count, IoUs)."""
  used: set[int] = set()
  ious: list[float] = []
  for expected in reference:
    best_index = -1
    best_iou = 0.0
    for index, actual in enumerate(candidate):
      if index in used or actual["class_id"] != expected.class_id:
        continue
      overlap = _iou(expected.box, actual["box"])
      if overlap > best_iou:
        best_iou = overlap
        best_index = index
    if best_index >= 0 and best_iou >= 0.5:
      used.add(best_index)
      ious.append(best_iou)
  return len(ious), ious


# ---------------------------------------------------------------------------
# export tool / names metadata (no GPU)
# ---------------------------------------------------------------------------

def test_export_tool_reads_onnx_metadata_and_writes_names(tmp_path):
  export = _tool("export_ncnn")
  metadata = export.read_onnx_metadata(DEFAULT_ONNX_MODEL)
  assert len(metadata["names"]) == 80
  assert metadata["names"][0] == "person"
  assert metadata["names"][2] == "car"
  assert metadata["names"][3] == "motorcycle"
  assert metadata["input_shape"] == [1, 3, 640, 640]
  assert metadata["imgsz"] == (640, 640)

  names_path = tmp_path / "yolo11n.ncnn.names.json"
  payload = export.write_names_json(metadata["names"], str(names_path))
  assert payload["0"] == "person"
  written = json.loads(names_path.read_text())
  assert written == payload

  if NCNN_MODELS_PRESENT:
    shipped = json.loads(open(DEFAULT_NCNN_NAMES, encoding="utf-8").read())
    assert written == shipped


def test_export_tool_command_and_skip_convert(tmp_path):
  export = _tool("export_ncnn")
  command = export.build_pnnx_command("/venv/bin/pnnx", "/models/yolo11n.onnx")
  assert command == ["/venv/bin/pnnx", "/models/yolo11n.onnx", "inputshape=in0=[1,3,640,640]"]
  assert export.build_pnnx_command("pnnx", "m.onnx", "images", "[1,3,320,320]")[-1] == "inputshape=images=[1,3,320,320]"

  output_dir = tmp_path / "models"
  exit_code = export.main([DEFAULT_ONNX_MODEL, "--out-dir", str(output_dir), "--skip-convert", "--prefix", "test_model"])
  assert exit_code == 0
  names_path = output_dir / "test_model.ncnn.names.json"
  assert names_path.is_file()
  names = json.loads(names_path.read_text())
  assert len(names) == 80
  assert names["3"] == "motorcycle"


def test_export_tool_dry_run_does_not_need_pnnx(tmp_path, capsys):
  export = _tool("export_ncnn")
  exit_code = export.main([DEFAULT_ONNX_MODEL, "--out-dir", str(tmp_path), "--dry-run", "--force"])
  assert exit_code == 0
  captured = capsys.readouterr()
  assert "inputshape=in0=[1,3,640,640]" in captured.out


# ---------------------------------------------------------------------------
# ncnn detector error paths (no net is created)
# ---------------------------------------------------------------------------

@requires_ncnn
@requires_ncnn_model
def test_missing_model_raises_before_touching_vulkan(tmp_path):
  from openpilot.sunnypilot.yolod.detector_ncnn import NcnnDetector
  with pytest.raises(FileNotFoundError):
    NcnnDetector(str(tmp_path / "missing.param"), DEFAULT_NCNN_BIN, names_path=DEFAULT_NCNN_NAMES)
  with pytest.raises(FileNotFoundError):
    NcnnDetector(DEFAULT_NCNN_PARAM, str(tmp_path / "missing.bin"), names_path=DEFAULT_NCNN_NAMES)


@requires_ncnn
def test_software_vulkan_device_is_rejected(monkeypatch):
  from openpilot.sunnypilot.yolod import detector_ncnn

  class _Info:
    def __init__(self, name: str):
      self._name = name

    def device_name(self) -> str:
      return self._name

  monkeypatch.setattr(detector_ncnn.ncnn, "get_gpu_count", lambda: 1)
  monkeypatch.setattr(detector_ncnn.ncnn, "get_gpu_info", lambda index: _Info("llvmpipe (LLVM 20.1.2, 256 bits)"))
  with pytest.raises(RuntimeError, match="software Vulkan"):
    detector_ncnn._select_vulkan_device()

  monkeypatch.setattr(detector_ncnn.ncnn, "get_gpu_count", lambda: 0)
  with pytest.raises(RuntimeError, match="no Vulkan device"):
    detector_ncnn._select_vulkan_device()


@requires_ncnn
@requires_ncnn_model
def test_vulkan_failure_is_not_silently_a_cpu_fallback(monkeypatch):
  from openpilot.sunnypilot.yolod import detector_ncnn

  def _no_vulkan() -> int:
    raise RuntimeError("ncnn Vulkan backend requested but no Vulkan device was found")

  monkeypatch.setattr(detector_ncnn, "_select_vulkan_device", _no_vulkan)
  with pytest.raises(RuntimeError, match="Vulkan"):
    detector_ncnn.NcnnDetector(DEFAULT_NCNN_PARAM, DEFAULT_NCNN_BIN, names_path=DEFAULT_NCNN_NAMES)


@requires_ncnn
@requires_ncnn_model
def test_names_file_matches_onnx_metadata():
  from openpilot.sunnypilot.yolod.detector_ncnn import load_class_names
  names = load_class_names(DEFAULT_NCNN_NAMES)
  assert len(names) == 80
  assert names[0] == "person"
  assert names == Detector(DEFAULT_ONNX_MODEL, threads=1).names


# ---------------------------------------------------------------------------
# GPU vs CPU equivalence, in a subprocess
# ---------------------------------------------------------------------------

@requires_ncnn
@requires_ncnn_model
@pytest.mark.skipif(not MEDIA_PRESENT, reason=f"night route recording not available at {NIGHT_VIDEO}")
def test_ncnn_matches_onnxruntime_on_real_frames(tmp_path):
  frames = 10
  helper_output = tmp_path / "ncnn.json"
  command = [sys.executable, "-m", "openpilot.sunnypilot.yolod.tests.ncnn_helper", NIGHT_ROUTE,
             "--max-frames", str(frames), "--out", str(helper_output), "--threads", "1"]
  result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=180, check=False)
  assert result.returncode == 0, f"ncnn helper failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr[-2000:]}"
  diagnostic = json.loads(helper_output.read_text())

  assert diagnostic["backend"] == "ncnn-vulkan"
  assert diagnostic["input"] == [640, 640]
  assert len(diagnostic["class_names"]) == 80
  assert diagnostic["class_names"][0] == "person"
  assert "llvmpipe" not in diagnostic["device"].lower()
  assert diagnostic["output"]["shape"] == [84, 8400]
  assert diagnostic["output"]["dtype"] == "float32"
  assert len(diagnostic["frames"]) == frames

  # Validate the serialized detections themselves, not just the counts.
  for frame in diagnostic["frames"]:
    assert frame["inference_ms"] > 0
    for detection in frame["detections"]:
      assert 0 <= detection["class_id"] < 80
      assert detection["label"] == diagnostic["class_names"][detection["class_id"]]
      assert 0.0 < detection["score"] <= 1.0
      x1, y1, x2, y2 = detection["box"]
      assert 0.0 <= x1 < x2 <= 1.0
      assert 0.0 <= y1 < y2 <= 1.0

  from openpilot.sunnypilot.yolod.video import iter_frames
  reference = Detector(DEFAULT_ONNX_MODEL, threads=4)
  reference.warmup()
  reference_detections = [reference.detect(image) for _, image in iter_frames(NIGHT_VIDEO, max_frames=frames)]
  assert len(reference_detections) == frames

  total_reference = total_ncnn = matched = 0
  ious: list[float] = []
  for expected, frame in zip(reference_detections, diagnostic["frames"], strict=True):
    candidates = frame["detections"]
    frame_matched, frame_ious = _match_frame(expected, candidates)
    matched += frame_matched
    ious.extend(frame_ious)
    total_reference += len(expected)
    total_ncnn += len(candidates)

  assert total_reference >= 20, "the test frames should contain enough objects"
  match_rate = matched / total_reference
  mean_iou = float(np.mean(ious)) if ious else 0.0
  count_difference = abs(total_reference - total_ncnn)
  assert match_rate >= 0.95, f"only {match_rate:.1%} of ORT detections matched ncnn"
  assert mean_iou >= 0.95, f"matched boxes have mean IoU {mean_iou:.3f}"
  assert count_difference <= max(3, round(0.05 * total_reference)), \
    f"detection counts differ: ORT {total_reference} vs ncnn {total_ncnn}"

  ncnn_times = sorted(frame["inference_ms"] for frame in diagnostic["frames"])
  ncnn_p50 = ncnn_times[len(ncnn_times) // 2]
  assert ncnn_p50 < 80.0, f"ncnn GPU inference is unexpectedly slow: p50 {ncnn_p50:.1f} ms"
  print(f"ncnn vs ORT: {total_ncnn} vs {total_reference}, matched {matched} ({match_rate:.1%}), IoU {mean_iou:.3f}, p50 {ncnn_p50:.1f} ms")
