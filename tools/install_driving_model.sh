#!/usr/bin/env bash
#
#
#   tools/install_driving_model.sh --onnx /path/to/model.onnx --name tsfdom
#   tools/install_driving_model.sh --onnx ... --name tsfdom --camera-resolutions "1344x760"
#   tools/install_driving_model.sh --onnx ... --name tsfdom --dry-run
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="$REPO/openpilot/selfdrive/modeld/models"
ONNX=""
NAME=""
CAMERA_RESOLUTIONS="1920x1080 1928x1208 1344x760"
DRY_RUN=0

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --onnx) ONNX="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --camera-resolutions) CAMERA_RESOLUTIONS="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$ONNX" ]] || { echo "缺少 --onnx" >&2; exit 2; }
[[ -f "$ONNX" ]] || { echo "ONNX 不存在: $ONNX" >&2; exit 2; }
[[ -n "$NAME" ]] || { echo "缺少 --name" >&2; exit 2; }

NAME="$(printf '%s' "$NAME" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_' '_' | sed -e 's/__*/_/g' -e 's/^_//' -e 's/_$//')"
[[ -n "$NAME" ]] || { echo "--name 至少要有一个字母或数字" >&2; exit 2; }

OUTPUT="$MODELS_DIR/amd_${NAME}.pkl"
[[ -x "$REPO/.venv/bin/python" ]] || { echo "找不到 $REPO/.venv/bin/python" >&2; exit 2; }

CMD=(env "DEV=${DEV:-KFD+AMD:LLVM}" FLOAT16=1
     "$REPO/.venv/bin/python" -m openpilot.selfdrive.modeld.compile_modeld
     --onnx "$ONNX" --model-size 512x256
     --camera-resolutions $CAMERA_RESOLUTIONS
     --frame-skip 4 --output "$OUTPUT")

if [[ $DRY_RUN -eq 1 ]]; then
  printf '%q ' "${CMD[@]}"; echo
  exit 0
fi

cd "$REPO"
echo "编译中（约 1~3 分钟）…"
"${CMD[@]}"
echo
echo "已安装: $OUTPUT"
echo "设置 → 模型 → 小模型 里会显示: $(printf '%s' "$NAME" | tr '[:lower:]' '[:upper:]')"
