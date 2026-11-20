"""Basemap CLI: bbox parsing and the platform asset name, without touching the network."""
import argparse

import pytest

from openpilot.tools import nav_basemap


class TestBbox:
  def test_accepts_south_west_north_east(self):
    assert nav_basemap.bbox_arg("30.4,105.8,31.4,106.9") == (30.4, 105.8, 31.4, 106.9)

  @pytest.mark.parametrize("text", ["1,2,3", "31,105,30,106", "31,106,30,105", "a,b,c,d"])
  def test_rejects_bad_boxes(self, text):
    with pytest.raises(argparse.ArgumentTypeError):
      nav_basemap.bbox_arg(text)


class TestPlatformTag:
  def test_tag_matches_the_release_naming(self):
    tag = nav_basemap._platform_tag()
    assert tag.startswith("_") and tag.endswith(".tar.gz")
    assert "Linux" in tag or "Darwin" in tag


class TestExtractGuard:
  def test_existing_file_needs_overwrite(self, tmp_path):
    out = tmp_path / "basemap.pmtiles"
    out.write_bytes(b"x")
    with pytest.raises(SystemExit):
      nav_basemap.extract((30.4, 105.8, 31.4, 106.9), 15, "https://example.com/x.pmtiles", out, False)
