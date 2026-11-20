"""Byte-range sources for PMTiles archives: a local extract, or a public URL.

Drop a Protomaps extract at `~/navigation/basemap.pmtiles` (or point
`SUNNYPILOT_NAV_TILES` at one), or give `SUNNYPILOT_NAV_TILES` an `https://...pmtiles`
URL. The reader only needs `size`, `read(offset, length)`, `prefetch(ranges)` and
`close()`, so both sources share the decoder.

The URL mode is for Protomaps' public daily build: a planet archive is 139 GB, but a tile
lookup costs the root directory (~15 KB), one leaf directory (~1 KB) and the tile itself,
all range requests. Fetched ranges are cached on disk so a repeated view is free.
"""
import re
import time
from pathlib import Path

CACHE_LIMIT = 64 * 1024 * 1024  # bytes of range cache for the HTTP source
MAX_PREFETCH_RANGES = 64  # a long route must not turn into thousands of range requests


class LocalTiles:
  def __init__(self, path: str | Path):
    self.path = Path(path)
    self.size = self.path.stat().st_size
    self._file = self.path.open("rb")

  def read(self, offset: int, length: int) -> bytes:
    self._file.seek(offset)
    return self._file.read(length)

  def prefetch(self, ranges) -> None:
    # A local file is already backed by the page cache; nothing to fetch ahead.
    return None

  def close(self) -> None:
    if not self._file.closed:
      self._file.close()


class HttpTiles:
  """Range requests against a public PMTiles URL, with a bounded on-disk cache."""

  def __init__(self, url: str, cache_dir: Path | None = None):
    import requests
    self.url = url
    self.session = requests.Session()
    probe = self.session.get(url, headers={"Range": "bytes=0-0"}, timeout=20)
    content_range = probe.headers.get("Content-Range", "")
    match = re.fullmatch(r"bytes 0-0/(\d+)", content_range)
    if probe.status_code != 206 or match is None:
      raise OSError(f"PMTiles URL is not range-readable ({probe.status_code})")
    self.size = int(match.group(1))
    self.cache_dir = Path(cache_dir) if cache_dir is not None else Path.home() / "navigation" / "tiles"
    self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

  def _cached(self, offset: int, length: int) -> Path:
    return self.cache_dir / f"{offset}-{length}"

  def read(self, offset: int, length: int) -> bytes:
    path = self._cached(offset, length)
    try:
      data = path.read_bytes()
      if len(data) == length:
        return data
    except FileNotFoundError:
      pass
    response = self.session.get(self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}"}, timeout=20)
    if response.status_code != 206:
      raise OSError(f"PMTiles range request failed ({response.status_code})")
    data = response.content
    if len(data) != length:
      raise OSError("PMTiles range response was truncated")
    try:
      path.write_bytes(data)
      self._trim()
    except OSError:
      pass  # a full cache must not break reading
    return data

  def _trim(self) -> None:
    files = [(entry.stat().st_mtime, entry.stat().st_size, entry)
             for entry in self.cache_dir.iterdir() if re.fullmatch(r"\d+-\d+", entry.name)]
    total = sum(size for _, size, _ in files)
    for _, size, entry in sorted(files):
      if total <= CACHE_LIMIT:
        break
      entry.unlink(missing_ok=True)
      total -= size

  def prefetch(self, ranges) -> None:
    """Warm the cache for a handful of ranges; the rest stay lazy on purpose."""
    started = time.monotonic()
    for index, (offset, length) in enumerate(sorted(set(ranges))):
      if index >= MAX_PREFETCH_RANGES or time.monotonic() - started > 2.0:
        break
      if self._cached(offset, length).exists():
        continue
      try:
        self.read(offset, length)
      except OSError:
        return

  def close(self) -> None:
    self.session.close()
