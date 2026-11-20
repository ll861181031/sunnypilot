from collections import namedtuple
import os
import time
from pathlib import Path
from collections.abc import Sequence

import openpilot.system.loggerd.deleter as deleter
from openpilot.system.loggerd.tests.loggerd_tests_common import UploaderTestCase

Stats = namedtuple("Stats", ['f_bavail', 'f_blocks', 'f_frsize'])


class TestDeleter(UploaderTestCase):
  # Deletion behavior is independent of file size; use smaller files to keep these tests fast.
  def make_file_with_data(self, f_dir: str, fn: str, size_mb: float = .001, lock: bool = False,
                          upload_xattr: bytes | None = None, preserve_xattr: bytes | None = None) -> Path:
    path = super().make_file_with_data(f_dir, fn, size_mb, lock, upload_xattr, preserve_xattr)
    os.utime(path, ns=(time.time_ns() - 1_000_000_000_000,) * 2)
    return path

  def fake_statvfs(self, d):
    return self.fake_stats

  def setup_method(self):
    self.f_type = "fcamera.hevc"
    super().openpilot_setup_method()
    self.fake_stats = Stats(f_bavail=0, f_blocks=10, f_frsize=4096)
    # deleter.os is the shared os module, so this replaces the global statvfs.
    # Restore it: a leaked fake (missing f_bfree et al.) breaks any later test in
    # the same process that calls os.statvfs / shutil.disk_usage.
    self.addCleanup(setattr, deleter.os, "statvfs", deleter.os.statvfs)
    deleter.os.statvfs = self.fake_statvfs  # ty: ignore[invalid-assignment]  # test double

  def test_delete(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type)
    assert deleter.deleter_step() == (True, str(f_path.parent))
    assert not f_path.exists()

  def assertDeleteOrder(self, f_paths: Sequence[Path]) -> None:
    routes = {}
    for path in f_paths:
      pair = deleter.split_segment(path.parent.name)
      routes.setdefault(pair[0] if pair else path.parent.name, []).append(path)
    for paths in routes.values():
      out_of_space, deleted_path = deleter.deleter_step()
      assert out_of_space and deleted_path is not None
      first = min(paths, key=lambda p: deleter.split_segment(p.parent.name)[1] if deleter.split_segment(p.parent.name) else 0)
      assert Path(deleted_path) == first.parent
      assert all(not p.exists() for p in paths), "Entire routes must be removed together"

  def test_delete_order(self):
    self.assertDeleteOrder([
      self.make_file_with_data(self.seg_format.format(0), self.f_type),
      self.make_file_with_data(self.seg_format.format(1), self.f_type),
      self.make_file_with_data(self.seg_format2.format(0), self.f_type),
    ])

  def test_delete_many_preserved(self):
    self.assertDeleteOrder([
      self.make_file_with_data(self.seg_format.format(0), self.f_type),
      self.make_file_with_data(self.seg_format.format(1), self.f_type, preserve_xattr=deleter.PRESERVE_ATTR_VALUE),
      self.make_file_with_data(self.seg_format.format(2), self.f_type),
    ] + [
      self.make_file_with_data(self.seg_format2.format(i), self.f_type, preserve_xattr=deleter.PRESERVE_ATTR_VALUE)
      for i in range(5)
    ])

  def test_delete_last(self):
    self.assertDeleteOrder([
      self.make_file_with_data(self.seg_format2.format(0), self.f_type),
      self.make_file_with_data(self.seg_format.format(0), self.f_type, preserve_xattr=deleter.PRESERVE_ATTR_VALUE),
      self.make_file_with_data(self.seg_format.format(1), self.f_type),
      self.make_file_with_data("boot", self.seg_format[:-4]),
      self.make_file_with_data("crash", self.seg_format2[:-4]),
    ])

  def test_no_delete_when_available_space(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type)

    block_size = 4096
    available = (10 * 1024 * 1024 * 1024) / block_size  # 10GB free
    self.fake_stats = Stats(f_bavail=available, f_blocks=10, f_frsize=block_size)

    assert deleter.deleter_step() == (False, None)
    assert f_path.exists(), "File deleted with available space"

  def test_no_delete_with_lock_file(self):
    f_path = self.make_file_with_data(self.seg_dir, self.f_type, lock=True)

    assert deleter.deleter_step() == (True, None)
    assert f_path.exists(), "File deleted when locked"
