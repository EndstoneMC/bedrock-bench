#!/usr/bin/env bash
# One locked bench run: run_one.sh <tag> <server-dir> <exe> [run_levi args...]; holds BENCH_LOCK as "levi-<tag>".
S="C:/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
L="$S/levi"
LOCK="$S/BENCH_LOCK"
TAG="$1"; SRV="$2"; EXE="$3"; shift 3

release() {
  if [[ "$(cat "$LOCK" 2>/dev/null)" == "levi-$TAG "* ]]; then
    rm -f "$LOCK"
    echo "lock released $(date +%T)"
  fi
}
trap release EXIT

until (set -o noclobber; echo "levi-$TAG $(date +%T)" > "$LOCK") 2>/dev/null; do
  echo "lock held by: $(cat "$LOCK" 2>/dev/null) at $(date +%T); waiting"
  sleep 15
done
echo "lock acquired $(date +%T): $(cat "$LOCK")"

cd "$L/harness" || exit 1
uv run --no-project --with psutil python -I run_levi.py --tag "$TAG" --vanilla --server "$SRV" --exe "$EXE" "$@" \
  > "$L/results/$TAG.out" 2>&1
rc=$?
echo "exit=$rc" >> "$L/results/$TAG.out"
died=$(uv run --no-project python -I -c "import json,sys;print(json.load(open(sys.argv[1])).get('died_during_run'))" \
  "$L/results/$TAG/results.json" 2>/dev/null)
echo "$TAG exit=$rc died=$died $(date +%T)"
[ "$rc" = 0 ] && [ "$died" = "False" ]
