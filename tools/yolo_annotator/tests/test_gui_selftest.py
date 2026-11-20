"""GUI smoke test: run the in-app selftest (needs an X display / xvfb)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]

#: Headless Linux: run the child under its own X server when possible, so a
#: plain ``pytest -q`` works without wrapping the whole session in xvfb-run.
XVFB_SCREEN = ["-a", "-s", "-screen 0 1280x800x24"]


def _display_prefix() -> list[str]:
    if sys.platform.startswith("win") or os.environ.get("DISPLAY"):
        return []
    xvfb_run = shutil.which("xvfb-run")
    if xvfb_run is None:
        pytest.skip("需要 X display（安装 xvfb-run 或设置 DISPLAY）")
    return [xvfb_run, *XVFB_SCREEN]


# ---------------------------------------------------------------------------
# Pure shortcut helpers
# ---------------------------------------------------------------------------
def test_hotkey_labels_cover_the_22_classes():
    import labeler

    assert labeler.hotkey_for_index(0) == "1"
    assert labeler.hotkey_for_index(8) == "9"
    assert labeler.hotkey_for_index(9) == "0"
    assert labeler.hotkey_for_index(10) == "Shift+1"
    assert labeler.hotkey_for_index(18) == "Shift+9"
    assert labeler.hotkey_for_index(19) == "Shift+0"
    assert labeler.hotkey_for_index(20) == "F1"
    assert labeler.hotkey_for_index(21) == "F2"
    assert labeler.hotkey_for_index(22) == ""


def test_class_index_from_key():
    import labeler

    assert labeler.class_index_from_key("1", False) == 0
    assert labeler.class_index_from_key("9", False) == 8
    assert labeler.class_index_from_key("0", False) == 9
    assert labeler.class_index_from_key("1", True) == 10
    assert labeler.class_index_from_key("0", True) == 19
    assert labeler.class_index_from_key("exclam", False) == 10
    assert labeler.class_index_from_key("parenright", False) == 19
    assert labeler.class_index_from_key("F1", False) == 20
    assert labeler.class_index_from_key("F2", False) == 21
    assert labeler.class_index_from_key("a", False) is None
    assert labeler.class_index_from_key("space", False) is None


# ---------------------------------------------------------------------------
# Selftest runs (each in a subprocess: one Tk interpreter per process, which is
# also how the app is really used; several Tk roots in one process can corrupt
# the Tcl interpreter on Windows).
# ---------------------------------------------------------------------------
def _run_selftest_subprocess(dataset_dir, screenshot=None, timeout=180):
    """Run ``labeler.py --selftest`` and return the CompletedProcess."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(APP_DIR)
    # Force UTF-8 in the child and decode explicitly: the Windows console code
    # page (cp936) cannot round-trip the Chinese selftest output.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    command = [*_display_prefix(), sys.executable, str(APP_DIR / "labeler.py"),
               "--selftest", "--dataset", str(dataset_dir)]
    if screenshot is not None:
        command += ["--screenshot", str(screenshot)]
    return subprocess.run(command, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          env=env)


def test_selftest_on_a_dataset(demo_batch, tmp_path):
    screenshot = tmp_path / "shot.png"
    process = _run_selftest_subprocess(demo_batch, screenshot)
    assert process.returncode == 0, process.stdout + process.stderr
    assert "SELFTEST OK" in process.stdout
    assert screenshot.is_file()
    assert screenshot.stat().st_size > 1000


def test_selftest_restores_the_dataset(demo_batch):
    """A selftest on a real batch must not leave the first image modified."""
    label_path = demo_batch / "labels" / "000001.txt"
    review_path = demo_batch / "review_state.json"
    before_label = label_path.read_bytes()
    before_review = review_path.read_bytes()

    process = _run_selftest_subprocess(demo_batch)
    assert process.returncode == 0, process.stdout + process.stderr

    # Byte identical: the selftest restores the files it touched.
    assert label_path.read_bytes() == before_label
    assert review_path.read_bytes() == before_review


def test_selftest_generates_a_demo_batch_when_missing(tmp_path):
    process = _run_selftest_subprocess(tmp_path / "no_dataset_here")
    assert process.returncode == 0, process.stdout + process.stderr
    assert "SELFTEST OK" in process.stdout
    assert "演示批次" in process.stdout


# ---------------------------------------------------------------------------
# Selftest in a subprocess exactly as CI / Windows does it
# ---------------------------------------------------------------------------
def test_selftest_prints_every_step(demo_batch):
    process = _run_selftest_subprocess(demo_batch)
    assert process.returncode == 0, process.stdout + process.stderr
    for step_name in ("数据集加载", "首图加载", "首帧渲染", "缩放", "画框",
                      "撤销", "重做", "改类别", "手柄缩放", "保存标签",
                      "N 无目标", "翻页", "筛选", "进度统计"):
        assert f"OK  {step_name}" in process.stdout, step_name


def test_help_lists_every_required_shortcut():
    import labeler

    for token in ("Ctrl+S", "Ctrl+Z", "Ctrl+Y", "Del", "A / ←", "D / →",
                  "滚轮", "空格", "F1", "Shift+0", "H "):
        assert token in labeler.HELP_TEXT, token
