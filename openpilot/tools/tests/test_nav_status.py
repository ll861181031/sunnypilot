"""nav_status: reading the drive audit out of modelDataV2SP."""
import pytest

from openpilot.tools.nav_status import summarize


class TestSummarize:
  def test_counts_valid_frames_and_ages(self):
    rows = [(True, True, 0.1, 0.1), (True, False, 0.2, 1.5), (False, False, 5.0, 2.5)]
    summary = summarize(rows)
    assert summary["frames"] == 3
    assert summary["map_valid_ratio"] == pytest.approx(2 / 3)
    assert summary["instruction_valid_ratio"] == pytest.approx(1 / 3)
    assert summary["map_age"]["p50"] == pytest.approx(0.2)
    assert summary["instruction_age"]["max"] == pytest.approx(2.5)

  def test_an_all_zero_drive_reads_as_un_navigated(self):
    summary = summarize([(False, False, 0.0, 0.0)] * 10)
    assert summary["map_valid_ratio"] == 0.0
    assert summary["instruction_valid_ratio"] == 0.0

  def test_empty_input(self):
    assert summarize([]) == {"frames": 0}
