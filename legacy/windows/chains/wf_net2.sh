#!/usr/bin/env bash
# Memory-fix build: wait for MEMFIX_BUILD_DONE (5 s poll), stage the DLL, rerun net-100's args as net2-100 under the
# network-bench2 lock; intest only if wf_net2_intest exists by then. Always releases the lock and touches MEMFIX_BENCH_DONE.
S="C:/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
B="$S/endstone-bench"
LOCK="$S/BENCH_LOCK"
ARM="dll-net2"
PERF_TAG="net2-100"
INTEST_TAG="intest-net2"
PERF_MARK="$S/wf_net2perf.done"
rm -f "$PERF_MARK"

finish() {
  if [ -f "$LOCK" ] && [[ "$(cat "$LOCK" 2>/dev/null)" == network-bench2* ]]; then
    rm -f "$LOCK"
    echo "lock released $(date +%T)"
  fi
  [ -f "$PERF_MARK" ] || echo "perf not run" > "$PERF_MARK"
  touch "$S/MEMFIX_BENCH_DONE"
  echo "MEMFIX_BENCH_DONE touched $(date +%T)"
}
trap finish EXIT

until [ -e "$S/MEMFIX_BUILD_DONE" ] || [ -e "$S/MEMFIX_BUILD_FAILED" ]; do sleep 5; done
echo "build marker seen $(date +%T)"
if [ ! -e "$S/MEMFIX_BUILD_DONE" ]; then
  echo "MEMFIX_BUILD_FAILED: $(cat "$S/MEMFIX_BUILD_FAILED")"
  echo "memfix build failed" > "$PERF_MARK"
  exit 2
fi
sleep 2

uv run --no-project python -I "$S/wf_stage.py" "$S" "$(date -d 'today 14:36' +%s)" MEMFIX_BUILD_DONE "$ARM" \
  || { echo "staging failed" > "$PERF_MARK"; exit 3; }

while ! (set -o noclobber; echo "network-bench2 $(date +%T)" > "$LOCK") 2>/dev/null; do
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

if [ -e "$S/wf_net2_intest" ]; then
  uv run --no-project --with psutil python -I run_endstone.py --tag "$INTEST_TAG" --arm "$ARM" --intest \
    > "$B/results/$INTEST_TAG.out" 2>&1
  rc=$?
  echo "exit=$rc" >> "$B/results/$INTEST_TAG.out"
  echo "intest exit $rc at $(date +%T)"
else
  echo "intest skipped"
fi
exit $rc
