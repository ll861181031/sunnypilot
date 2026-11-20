"""Local trip statistics, shared by the Trips page and the trip-stats CLI.

The cloud drive stats need a paired device (`DongleId`); on a desktop or
unregistered device the Trips page would be empty forever. This module
aggregates the same fields straight from the on-disk recordings so the page
has data on any machine.

Distance is reported in **miles** to match the cloud response shape that the
Trips page renders (`{"all": {...}, "week": {...}}` with `routes`/`distance`/
`minutes`).
"""
import hashlib
import os
import re
import stat
import time
from pathlib import Path

ROUTE_SEGMENT_RE = re.compile(r"^([0-9a-f]+--[0-9a-f]+)--(\d+)$")

METERS_PER_MILE = 1609.344
CACHE_PARAM = "LocalDriveStats"

# Param writes propagate over zmq, so a value written a moment ago may not be
# readable yet. Remember the last scan in-process to avoid rescanning on the
# next tick of the same (long-lived) process.
_MEMO: dict = {}


def raw_log_root() -> str:
  from openpilot.common.hardware.hw import Paths
  return Paths.log_root()


class Segment:
  """Per-segment (one minute-ish) numbers read from a single log file."""

  __slots__ = ("route", "index", "path", "wall_start", "wall_end", "span",
               "dist", "vmax", "mads", "lat", "long_", "engaged", "acc",
               "stand", "brakes", "gas", "steer", "n_car")

  def __init__(self, route: str, index: int, path: Path):
    self.route, self.index, self.path = route, index, path
    self.wall_start = self.wall_end = path.stat().st_mtime
    self.span = self.dist = self.vmax = 0.0
    self.mads = self.lat = self.long_ = self.engaged = self.acc = self.stand = 0.0
    self.brakes = self.gas = self.steer = 0
    self.n_car = 0


class Route:
  """One drive: all the segments that share a route id, oldest first."""

  def __init__(self, route: str):
    self.route = route
    self.segs: list[Segment] = []

  def add(self, seg: Segment) -> None:
    self.segs.append(seg)

  @property
  def wall_start(self) -> float:
    return min(s.wall_start for s in self.segs)

  @property
  def wall_end(self) -> float:
    return max(s.wall_end for s in self.segs)

  @property
  def span(self) -> float:
    return sum(s.span for s in self.segs)

  @property
  def dist(self) -> float:
    return sum(s.dist for s in self.segs)

  def total(self, attr: str) -> float:
    return sum(getattr(s, attr) for s in self.segs)


def analyse_segment(path: Path, route: str, index: int) -> "Segment | None":
  """Read one log file and return its numbers, or None when unreadable."""
  from openpilot.tools.lib.logreader import LogReader

  try:
    seg = Segment(route, index, path)
  except FileNotFoundError:
    return None
  wall_epoch: float | None = None              # wall = wall_epoch + logMonoTime
  t_first = t_last = None
  prev: dict[str, tuple[float, float]] = {}    # stream -> (t, value)
  prev_bool: dict[str, bool] = {}              # for edge detection
  try:
    for msg in LogReader(str(path)):
      t = msg.logMonoTime / 1e9
      w = msg.which()
      if w == "initData":
        # initData carries the real wall clock; its own logMonoTime is the
        # log creation time, not the start of the data.
        if msg.initData.wallTimeNanos:
          wall_epoch = msg.initData.wallTimeNanos / 1e9 - t
        continue
      if t_first is None:
        t_first = t
      t_last = t

      if w == "carState":
        cs = msg.carState
        v = float(cs.vEgo)
        seg.n_car += 1
        seg.vmax = max(seg.vmax, v)
        pt = prev.get("carState")
        if pt is not None:
          dt = t - pt[0]
          if 0.0 < dt < 1.5:
            seg.dist += (v + pt[1]) / 2.0 * dt
            seg.span += dt
            if v < 0.1:
              seg.stand += dt
        prev["carState"] = (t, v)
        for name, val in (("brake", cs.brakePressed), ("gas", cs.gasPressed),
                          ("steer", cs.steeringPressed)):
          key = {"brake": "brakes", "gas": "gas", "steer": "steer"}[name]
          if prev_bool.get(name) is False and val:
            setattr(seg, key, getattr(seg, key) + 1)
          prev_bool[name] = bool(val)
        pt = prev.get("acc")
        if cs.cruiseState.enabled:
          if pt is not None and 0.0 < t - pt[0] < 1.5:
            seg.acc += t - pt[0]
          prev["acc"] = (t, 1.0)
        else:
          prev["acc"] = (t, 0.0)

      elif w == "carControl":
        cc = msg.carControl
        for key, attr, val in (("lat", "lat", bool(cc.latActive)),
                               ("long", "long_", bool(cc.longActive)),
                               ("enabled", "engaged", bool(cc.enabled))):
          pt = prev.get(key)
          if pt is not None and 0.0 < t - pt[0] < 1.5 and pt[1]:
            setattr(seg, attr, getattr(seg, attr) + (t - pt[0]))
          prev[key] = (t, 1.0 if val else 0.0)

      elif w == "carControlSP":
        active = bool(msg.carControlSP.mads.active)
        pt = prev.get("mads")
        if pt is not None and 0.0 < t - pt[0] < 1.5 and pt[1]:
          seg.mads += t - pt[0]
        prev["mads"] = (t, 1.0 if active else 0.0)
  except Exception:
    return None                                # corrupt / half-written segment

  if t_first is None or t_last is None or seg.n_car == 0:
    return None
  if wall_epoch is None:
    seg.wall_start = seg.wall_end - seg.span
  else:
    seg.wall_start = wall_epoch + t_first
    seg.wall_end = wall_epoch + t_last
  return seg


def _segment_files(root, log_name):
  root = Path(root) if root is not None else Path(raw_log_root())
  out = []
  try:
    directories = list(root.iterdir())
  except (FileNotFoundError, NotADirectoryError):
    return out
  for d in directories:
    m = ROUTE_SEGMENT_RE.match(d.name)
    if not m:
      continue
    for name in (log_name, "qlog.zst" if log_name == "rlog.zst" else "rlog.zst"):
      log = d / name
      try:
        info = log.stat()
      except (FileNotFoundError, NotADirectoryError):
        continue
      if stat.S_ISREG(info.st_mode):
        out.append((m.group(1), int(m.group(2)), log, info))
        break
  out.sort(key=lambda x: (x[3].st_mtime_ns, x[2]))
  return out


def list_segments(root: "str | os.PathLike | None" = None, log_name: str = "qlog.zst",
                  ) -> "list[tuple[str, int, Path]]":
  """Return (route, index, log path) for every segment, oldest first."""
  return [(route, index, path) for route, index, path, _ in _segment_files(root, log_name)]


def scan_segments(root: "str | os.PathLike | None" = None, log_name: str = "qlog.zst",
                  ) -> list[Segment]:
  segs = []
  for route, index, log in list_segments(root, log_name):
    seg = analyse_segment(log, route, index)
    if seg is not None:
      segs.append(seg)
  return segs


def newest_segment_mtime(root: "str | os.PathLike | None" = None) -> float:
  files = _segment_files(root, "qlog.zst")
  return files[-1][3].st_mtime if files else 0.0


def group_routes(segs: list[Segment]) -> list[Route]:
  routes: dict[str, Route] = {}
  for seg in segs:
    routes.setdefault(seg.route, Route(seg.route)).add(seg)
  for route in routes.values():
    route.segs.sort(key=lambda x: x.index)
  return sorted(routes.values(), key=lambda r: r.wall_start)


def to_cloud_shape(routes: list[Route], now: "float | None" = None,
                   week_days: int = 7) -> dict:
  """Aggregate routes into the cloud-stats shape the Trips page renders."""
  # Compare against recording mtimes, which live on the wall clock, so a
  # monotonic clock would be wrong here.
  now = time.time() if now is None else now  # noqa: TID251
  cutoff = now - week_days * 86400

  def total(ds: list[Route]) -> dict:
    return {
      "routes": len(ds),
      "distance": round(sum(r.dist for r in ds) / METERS_PER_MILE, 2),
      "minutes": round(sum(r.span for r in ds) / 60.0, 1),
    }

  return {
    "all": total(routes),
    "week": total([r for r in routes if r.wall_end >= cutoff]),
  }


def local_drive_stats(root: "str | os.PathLike | None" = None,
                      week_days: int = 7) -> dict:
  """Full cloud-shaped stats computed from the on-disk recordings."""
  return to_cloud_shape(group_routes(scan_segments(root)), week_days=week_days)


def local_drive_stats_cached(params, root: "str | os.PathLike | None" = None,
                             week_days: int = 7, force: bool = False) -> "dict | None":
  """日志清单和周统计窗口均未变化时，直接使用已计算的行程统计。"""
  root = Path(raw_log_root() if root is None else root).resolve()
  if not root.is_dir():
    return None
  files = _segment_files(root, "qlog.zst")
  digest = hashlib.sha256()
  for _, _, path, info in files:
    digest.update(f"{path.parent.name}/{path.name}:{info.st_size}:{info.st_mtime_ns}\n".encode())
  signature = digest.hexdigest()
  now = time.time()  # noqa: TID251
  if not force:
    try:
      persisted = params.get(CACHE_PARAM)
    except Exception:
      persisted = None
    for cached in (_MEMO.get(str(root)), persisted):
      if (isinstance(cached, dict) and cached.get("signature") == signature and cached.get("root") == str(root)
          and cached.get("weekDays") == week_days and cached["computedAt"] <= now
          and (cached["validUntil"] is None or now <= cached["validUntil"])):
        return cached["stats"]

  routes = group_routes([seg for r, i, path, _ in files if (seg := analyse_segment(path, r, i)) is not None])
  fresh = to_cloud_shape(routes, now=now, week_days=week_days)
  cutoff = now - week_days * 86400
  valid_until = min((r.wall_end + week_days * 86400 for r in routes if r.wall_end >= cutoff), default=None)
  entry = {"root": str(root), "signature": signature, "weekDays": week_days, "computedAt": now, "validUntil": valid_until, "stats": fresh}
  _MEMO[str(root)] = entry
  try:
    params.put(CACHE_PARAM, entry)
  except Exception:
    pass
  return fresh
