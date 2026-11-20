#!/usr/bin/env python3
"""Build the pinned mapd release for Linux x86_64 without replacing the ARM binary."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from openpilot.sunnypilot.mapd import VERSION
from openpilot.sunnypilot.mapd.runtime import NATIVE_MAPD_PATH

SOURCE_URL = f"https://codeload.github.com/pfeiferj/mapd/tar.gz/refs/tags/{VERSION}"
SOURCE_SHA256 = "16273e06274a06eb53cddeb661332fb9cff324e479c09452b7237cf058936994"
PATCH = Path(__file__).with_name("native_paths.patch")


def sha256(path: Path) -> str:
  with path.open("rb") as f:
    return hashlib.file_digest(f, "sha256").hexdigest()


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--go", default=shutil.which("go"), help="Go 1.20+ compiler (upstream builds with 1.24)")
  parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/sunnypilot/mapd" / VERSION)
  args = parser.parse_args()
  if platform.system() != "Linux" or platform.machine() != "x86_64":
    parser.error("This build target requires a Linux x86_64 host")
  if not args.go:
    parser.error("Go not found; pass --go /absolute/path/to/go")
  go = str(Path(args.go).resolve())
  args.cache.mkdir(parents=True, exist_ok=True)
  archive = args.cache / "source.tar.gz"
  if not archive.exists():
    with urllib.request.urlopen(SOURCE_URL, timeout=60) as response, archive.with_suffix(".tmp").open("wb") as out:
      shutil.copyfileobj(response, out)
    archive.with_suffix(".tmp").replace(archive)
  if sha256(archive) != SOURCE_SHA256:
    raise RuntimeError(f"mapd source checksum mismatch: {archive}")

  env = dict(os.environ, CGO_ENABLED="0", GOOS="linux", GOARCH="amd64", GOAMD64="v1", GOTOOLCHAIN="local")
  go_version = subprocess.check_output([go, "version"], env=env, text=True).strip()
  with tempfile.TemporaryDirectory(prefix="build-", dir=args.cache) as temp:
    with tarfile.open(archive) as source:
      source.extractall(temp, filter="data")
    source_dir = Path(temp) / f"mapd-{VERSION.removeprefix('v')}"
    subprocess.run(["patch", "--batch", "-p1", "-i", str(PATCH)], cwd=source_dir, check=True)
    for command in ([go, "mod", "download"], [go, "mod", "verify"], [go, "test", "-mod=readonly", "./..."],
                    [go, "build", "-mod=readonly", "-trimpath", "-ldflags=-s -w", "-o", str(Path(temp) / "mapd"), "."]):
      subprocess.run(command, cwd=source_dir, env=env, check=True)
    output = Path(temp) / "mapd"
    subprocess.run([str(output), "-h"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    metadata = {
      "version": VERSION, "source_url": SOURCE_URL, "source_sha256": SOURCE_SHA256,
      "patch_sha256": sha256(PATCH), "go_version": go_version, "goos": "linux", "goarch": "amd64", "goamd64": "v1",
      "cgo_enabled": False, "sha256": sha256(output),
    }
    # Install on the same filesystem so replacement is atomic.
    staged = NATIVE_MAPD_PATH.with_suffix(".tmp")
    shutil.copyfile(output, staged)
    staged.chmod(0o755)
    staged.replace(NATIVE_MAPD_PATH)
    staged_meta = NATIVE_MAPD_PATH.with_suffix(".json.tmp")
    staged_meta.write_text(json.dumps(metadata, indent=2) + "\n")
    staged_meta.replace(NATIVE_MAPD_PATH.with_suffix(".json"))
  print(f"Built {NATIVE_MAPD_PATH}\nSHA256: {metadata['sha256']}")


if __name__ == "__main__":
  main()
