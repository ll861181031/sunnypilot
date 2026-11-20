#!/usr/bin/env bash
# Build one dataset batch end to end: extract -> autolabel -> build -> validate.
#
# Designed to be launched detached so a long run survives the shell:
#   setsid nohup tools/yolo_dataset/run_batch.sh > /tmp/yolo_batch.log 2>&1 &
#   echo $! > /tmp/yolo_batch.pid
#
# Every stage is idempotent: re-running skips routes that already have complete
# frames and metadata, so an interrupted batch resumes where it stopped.
#
# Defaults build the mixed 12k batch. The night supplement is the same script with
# a denser sampling of the night routes only:
#   BATCH=batch_002_night WORK=/data/yolo_dataset_night HOURS=night FPS=1.0 \
#   TARGET=5000 DAY_FRAC=0.0 PER_ROUTE_CAP=60 SAMPLE_FPS=1.0
set -uo pipefail

PY=${PY:-/data/sunnypilot/.venv/bin/python}
TOOLS=${TOOLS:-/data/sunnypilot/tools/yolo_dataset}
WORK=${WORK:-/data/yolo_dataset}
OUT=${OUT:-$WORK}
BATCH=${BATCH:-batch_001}
TARGET=${TARGET:-12000}
WORKERS=${WORKERS:-8}
FPS=${FPS:-0.5}
HOURS=${HOURS:-all}
DAY_FRAC=${DAY_FRAC:-0.5}
NEG_FRAC=${NEG_FRAC:-0.1}
PER_ROUTE_CAP=${PER_ROUTE_CAP:-40}
SAMPLE_FPS=${SAMPLE_FPS:-$FPS}
# Optional dataset.json whose route->split assignment must be reused, so a later
# batch never puts a route in val that an earlier batch put in train.
SPLIT_FROM=${SPLIT_FROM:-}
CLEAN_FRAMES=${CLEAN_FRAMES:-1}

SPLIT_ARG=()
if [ -n "$SPLIT_FROM" ]; then
  SPLIT_ARG=(--split-from "$SPLIT_FROM")
fi

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { log "FAILED: $*"; exit 1; }

free_gb() { df --output=avail -BG "$OUT" | tail -1 | tr -dc '0-9'; }

cd "$TOOLS" || die "no tool dir $TOOLS"
mkdir -p "$WORK" "$OUT" || die "cannot create $WORK / $OUT"
log "start batch=$BATCH target=$TARGET workers=$WORKERS fps=$FPS hours=$HOURS"
log "      work=$WORK out=$OUT free=$(free_gb)GB"

started=$(date +%s)

log "stage 1/4 extract"
"$PY" extract.py --work "$WORK" --workers "$WORKERS" --fps "$FPS" --hours "$HOURS" \
  || die "extract"
log "extract done, _frames=$(du -sh "$WORK/_frames" 2>/dev/null | cut -f1) free=$(free_gb)GB"

log "stage 2/4 autolabel"
"$PY" autolabel.py --work "$WORK" --workers "$WORKERS" || die "autolabel"
log "autolabel done, free=$(free_gb)GB"

log "stage 3/4 build"
"$PY" build.py --work "$WORK" --out "$OUT" --batch "$BATCH" --target "$TARGET" \
  --day-frac "$DAY_FRAC" --neg-frac "$NEG_FRAC" --per-route-cap "$PER_ROUTE_CAP" \
  --sample-fps "$SAMPLE_FPS" --workers "$WORKERS" --overwrite --package \
  ${SPLIT_ARG[@]+"${SPLIT_ARG[@]}"} || die "build"
log "build done, $(du -sh "$OUT/$BATCH" 2>/dev/null | cut -f1) free=$(free_gb)GB"

log "stage 4/4 validate"
"$PY" validate.py --dataset "$OUT/$BATCH" || die "validate"

if [ "$CLEAN_FRAMES" = "1" ]; then
  # The delivered batch is self-contained; raw frames are the only big temp data.
  log "cleaning $WORK/_frames ($(du -sh "$WORK/_frames" 2>/dev/null | cut -f1))"
  rm -rf "$WORK/_frames"
fi

elapsed=$(( $(date +%s) - started ))
log "DONE batch=$BATCH in ${elapsed}s ($(echo "scale=1; $elapsed/60" | bc) min) free=$(free_gb)GB"
log "dataset=$OUT/$BATCH"
log "archive=$OUT/$BATCH.tar.gz ($(du -h "$OUT/$BATCH.tar.gz" 2>/dev/null | cut -f1))"
