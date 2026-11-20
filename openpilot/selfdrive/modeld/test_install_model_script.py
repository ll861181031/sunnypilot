
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "tools" / "install_driving_model.sh"


def _run(*args):
  return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, timeout=30)


def test_dry_run_targets_the_local_model_folder(tmp_path):
  onnx = tmp_path / "tsfdom.onnx"
  onnx.write_bytes(b"x")
  out = _run("--onnx", str(onnx), "--name", "TSFDOM", "--dry-run")
  assert out.returncode == 0, out.stderr
  assert "amd_tsfdom.pkl" in out.stdout
  assert "--camera-resolutions 1920x1080 1928x1208 1344x760" in out.stdout
  assert "--model-size 512x256" in out.stdout
  assert "--frame-skip 4" in out.stdout


def test_name_is_normalised_for_the_ref(tmp_path):
  onnx = tmp_path / "m.onnx"
  onnx.write_bytes(b"x")
  out = _run("--onnx", str(onnx), "--name", "My Model!", "--dry-run")
  assert out.returncode == 0, out.stderr
  assert "amd_my_model.pkl" in out.stdout


def test_a_name_without_letters_is_rejected(tmp_path):
  onnx = tmp_path / "m.onnx"
  onnx.write_bytes(b"x")
  out = _run("--onnx", str(onnx), "--name", "!!!", "--dry-run")
  assert out.returncode == 2
  assert "name" in out.stderr.lower() or "名字" in out.stderr


def test_missing_onnx_is_rejected(tmp_path):
  out = _run("--onnx", str(tmp_path / "nope.onnx"), "--name", "x", "--dry-run")
  assert out.returncode == 2
  assert "ONNX" in out.stderr
