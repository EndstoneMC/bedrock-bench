#!/usr/bin/env bash
# Windows release validation: intest, then a 100-bot smoke run with net2-100 args, under the final-win lock.
S="C:/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
B="$S/endstone-bench"
LOCK="$S/BENCH_LOCK"
ARM="dll-final"
PERF_TAG="final-100"
INTEST_TAG="intest-final"
MARK="$S/wf_final.done"
rm -f "$MARK"

finish() {
  if [ -f "$LOCK" ] && [[ "$(cat "$LOCK" 2>/dev/null)" == final-win* ]]; then
    rm -f "$LOCK"
    echo "lock released $(date +%T)"
  fi
  [ -f "$MARK" ] || echo "ended early" > "$MARK"
}
trap finish EXIT

while ! (set -o noclobber; echo "final-win $(date +%T)" > "$LOCK") 2>/dev/null; do
  echo "lock held by: $(cat "$LOCK" 2>/dev/null) at $(date +%T); retrying"
  sleep 5
done
echo "lock acquired $(date +%T): $(cat "$LOCK")"
cd "$B" || exit 1

uv run --no-project --with psutil python -I run_endstone.py --tag "$INTEST_TAG" --arm "$ARM" --intest \
  > "$B/results/$INTEST_TAG.out" 2>&1
rc=$?
echo "exit=$rc" >> "$B/results/$INTEST_TAG.out"
verdict=$(uv run --no-project python -I -c "import json,sys;print(json.load(open(sys.argv[1]))['verdict'])" \
  "$B/results/$INTEST_TAG/intest.json" 2>/dev/null)
echo "intest exit $rc verdict=$verdict at $(date +%T)"
if [ "$rc" != 0 ] || [ "$verdict" != "PASS" ]; then
  echo "intest failed: rc=$rc verdict=$verdict" > "$MARK"
  exit 1
fi

uv run --no-project --with psutil python -I run_endstone.py --tag "$PERF_TAG" --arm "$ARM" \
  --storm-prof 60 --steady-prof 0 --min-settle 300 --max-settle 900 --no-prof > "$B/results/$PERF_TAG.out" 2>&1
rc=$?
echo "exit=$rc" >> "$B/results/$PERF_TAG.out"
died=$(uv run --no-project python -I -c "import json,sys;print(json.load(open(sys.argv[1])).get('died_during_run'))" \
  "$B/results/$PERF_TAG/results.json" 2>/dev/null)
echo "perf exit $rc died=$died at $(date +%T)"
echo "intest PASS; perf exit=$rc died=$died $(date +%T)" > "$MARK"
[ "$rc" = 0 ] && [ "$died" = "False" ]
