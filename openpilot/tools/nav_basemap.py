#!/usr/bin/env python3
"""Download and slice an offline PMTiles basemap for the nav model.

`mapsd` renders the model's 256x256 map from a PMTiles archive. Without one it can only
draw the route line. This tool fetches the `pmtiles` CLI, extracts a bounding box from
Protomaps' daily planet build, and points the `NavTiles` Param at the result so the car
works without internet.

  python -m openpilot.tools.nav_basemap status
  python -m openpilot.tools.nav_basemap extract --bbox 30.4,105.8,31.4,106.9 --maxzoom 15
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

RELEASES = "https://github.com/protomaps/go-pmtiles/releases/latest/download"
RELEASE_API = "https://api.github.com/repos/protomaps/go-pmtiles/releases/latest"
DEFAULT_URL = "https://build.protomaps.com/20261106.pmtiles"
DEFAULT_OUT = Path.home() / "navigation" / "basemap.pmtiles"
CLI_PATH = Path.home() / ".local" / "bin" / "pmtiles"
TILES_PARAM = "NavTiles"


def _platform_tag() -> str:
  """Suffix shared by every release asset for this platform (the version is in the name)."""
  system = platform.system().lower()
  machine = platform.machine().lower()
  os_name = {"linux": "Linux", "darwin": "Darwin"}.get(system)
  arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
  if os_name is None or arch is None:
    raise SystemExit(f"unsupported platform: {system}/{machine}")
  return f"_{os_name}_{arch}.tar.gz"


def _asset_url() -> str:
  """Release assets carry the version in the name, so ask the API for the exact URL."""
  tag = _platform_tag()
  try:
    with urllib.request.urlopen(RELEASE_API, timeout=30) as response:
      payload = json.loads(response.read())
    for asset in payload.get("assets", []):
      if asset.get("name", "").endswith(tag):
        return asset["browser_download_url"]
  except Exception:
    pass
  raise SystemExit(f"找不到适用于本平台的 pmtiles 资产（后缀 {tag}）")


def ensure_cli() -> Path:
  if CLI_PATH.is_file():
    return CLI_PATH
  CLI_PATH.parent.mkdir(parents=True, exist_ok=True)
  url = _asset_url()
  print(f"下载 pmtiles CLI: {url}")
  with urllib.request.urlopen(url, timeout=120) as response, \
       tarfile.open(fileobj=response, mode="r|gz") as archive:
    for member in archive:
      if member.name.endswith("pmtiles") and member.isfile():
        extracted = archive.extractfile(member)
        CLI_PATH.write_bytes(extracted.read())
        CLI_PATH.chmod(0o755)
        break
  if not CLI_PATH.is_file():
    raise SystemExit("pmtiles CLI 下载失败：压缩包里没有 pmtiles")
  return CLI_PATH


def bbox_arg(text: str) -> tuple[float, float, float, float]:
  try:
    south, west, north, east = (float(v) for v in text.split(","))
  except ValueError:
    raise argparse.ArgumentTypeError("bbox 需要 south,west,north,east") from None
  if not (south < north and west < east):
    raise argparse.ArgumentTypeError("bbox 需要 south<north 且 west<east")
  return south, west, north, east


def extract(bbox, maxzoom: int, url: str, out: Path, overwrite: bool) -> dict:
  if out.exists() and not overwrite:
    raise SystemExit(f"{out} 已存在；加 --overwrite 重新生成")
  cli = ensure_cli()
  out.parent.mkdir(parents=True, exist_ok=True)
  south, west, north, east = bbox
  command = [str(cli), "extract", url, str(out),
             f"--bbox={west},{south},{east},{north}", f"--maxzoom={maxzoom}"]
  print("执行:", " ".join(command))
  subprocess.run(command, check=True)
  size = out.stat().st_size
  print(f"完成: {out} ({size / (1024 ** 2):.1f} MB)")
  return {"path": str(out), "size_mb": round(size / (1024 ** 2), 1), "maxzoom": maxzoom,
          "bbox": list(bbox)}


def set_param(path: Path) -> None:
  sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
  from openpilot.common.params import Params
  Params().put(TILES_PARAM, str(path))
  print(f"已把 {TILES_PARAM} 指向 {path}（下次起栈 mapsd 生效）")


def status() -> None:
  sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
  from openpilot.common.params import Params
  params = Params()
  value = params.get(TILES_PARAM)
  if isinstance(value, bytes):
    value = value.decode()
  local = Path(value).expanduser() if value else DEFAULT_OUT
  info = {"param": value, "env": os.environ.get("SUNNYPILOT_NAV_TILES"),
          "local": str(local), "exists": local.is_file()}
  if local.is_file():
    info["size_mb"] = round(local.stat().st_size / (1024 ** 2), 1)
  cli_ok = CLI_PATH.is_file()
  print(json.dumps({**info, "pmtiles_cli": str(CLI_PATH) if cli_ok else None}, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  sub = parser.add_subparsers(dest="command", required=True)
  sub.add_parser("status", help="show current basemap source")
  extract_parser = sub.add_parser("extract", help="slice a bbox into a local archive")
  extract_parser.add_argument("--bbox", type=bbox_arg, required=True, help="south,west,north,east")
  extract_parser.add_argument("--maxzoom", type=int, default=15)
  extract_parser.add_argument("--url", default=DEFAULT_URL)
  extract_parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
  extract_parser.add_argument("--overwrite", action="store_true")
  extract_parser.add_argument("--no-param", action="store_true", help="do not write the NavTiles Param")
  args = parser.parse_args(argv)

  if args.command == "status":
    status()
    return 0
  result = extract(args.bbox, args.maxzoom, args.url, args.out.expanduser(), args.overwrite)
  print(json.dumps(result, ensure_ascii=False))
  if not args.no_param:
    set_param(args.out.expanduser())
  return 0


if __name__ == "__main__":
  sys.exit(main())
