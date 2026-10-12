#!/usr/bin/env bash
# Worker-wake threshold 0.01 s: net2-100 args as wake01-100 under the wake01-bench lock, no intest.
# Always releases the lock and touches WAKE01_WIN_DONE.
S="C:/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
B="$S/endstone-bench"
LOCK="$S/BENCH_LOCK"
ARM="dll-wake01"
PERF_TAG="wake01-100"
PERF_MARK="$S/wf_wake01perf.done"
rm -f "$PERF_MARK"

finish() {
  if [ -f "$LOCK" ] && [[ "$(cat "$LOCK" 2>/dev/null)" == wake01-bench* ]]; then
    rm -f "$LOCK"
    echo "lock released $(date +%T)"
  fi
  [ -f "$PERF_MARK" ] || echo "perf not run" > "$PERF_MARK"
  touch "$S/WAKE01_WIN_DONE"
  echo "WAKE01_WIN_DONE touched $(date +%T)"
}
trap finish EXIT

while ! (set -o noclobber; echo "wake01-bench $(date +%T)" > "$LOCK") 2>/dev/null; do
  echo "lock held by: $(cat "$LOCK" 2>/dev/null) at $(date +%T); retrying"
  sleep 5
done
echo "lock acquired $(date +%T): $(cat "$LOCK")"

cd "$B" || exit 1
uv run --no-project --with psutil python -I run_endstone.py --tag "$PERF_TAG" --arm "$ARM" \
  --storm-prof 60 --steady-prof 0 --min-settle 300 --max-settle 900 --no-prof > "$B/results/$PERF_TAG.out" 2>&1
rc=$?
echo "exit=$rc" >> "$B/results/$PERF_TAG.out"
died=$(uv run --no-project python -I -c "import json,sys;print(json.load(open(sys.argv[1])).get('died_during_run'))" \
  "$B/results/$PERF_TAG/results.json" 2>/dev/null)
echo "perf exit $rc died=$died at $(date +%T)"
echo "perf exit=$rc died=$died $(date +%T)" > "$PERF_MARK"
if [ "$rc" != 0 ] || [ "$died" != "False" ]; then
  echo "perf run failed"
  exit 1
fi

exit 0
