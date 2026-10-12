#!/usr/bin/env bash
# Network build: wait for NETBUILD_DONE (5 s poll), stage the DLL, then a 100-bot perf run like after-100 and intest
# under one network-bench lock. Always releases the lock and touches NETBENCH_DONE.
S="C:/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
B="$S/endstone-bench"
LOCK="$S/BENCH_LOCK"
ARM="dll-net"
PERF_TAG="net-100"
INTEST_TAG="intest-net"
PERF_MARK="$S/wf_netperf.done"
rm -f "$PERF_MARK"

finish() {
  if [ -f "$LOCK" ] && [[ "$(cat "$LOCK" 2>/dev/null)" == network-bench* ]]; then
    rm -f "$LOCK"
    echo "lock released $(date +%T)"
  fi
  [ -f "$PERF_MARK" ] || echo "perf not run" > "$PERF_MARK"
  touch "$S/NETBENCH_DONE"
  echo "NETBENCH_DONE touched $(date +%T)"
}
trap finish EXIT

until [ -e "$S/NETBUILD_DONE" ] || [ -e "$S/NETBUILD_FAILED" ]; do sleep 5; done
echo "build marker seen $(date +%T)"
if [ -e "$S/NETBUILD_FAILED" ] && [ ! -e "$S/NETBUILD_DONE" ]; then
  echo "NETBUILD_FAILED: $(cat "$S/NETBUILD_FAILED")"
  echo "netbuild failed" > "$PERF_MARK"
  exit 2
fi
sleep 2

uv run --no-project python -I "$S/wf_stage.py" "$S" "$(date -d 'today 13:00' +%s)" || { echo "staging failed" > "$PERF_MARK"; exit 3; }

while ! (set -o noclobber; echo "network-bench $(date +%T)" > "$LOCK") 2>/dev/null; do
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
  echo "perf run failed; skipping intest"
  exit 1
fi

uv run --no-project --with psutil python -I run_endstone.py --tag "$INTEST_TAG" --arm "$ARM" --intest \
  > "$B/results/$INTEST_TAG.out" 2>&1
rc=$?
echo "exit=$rc" >> "$B/results/$INTEST_TAG.out"
echo "intest exit $rc at $(date +%T)"
exit $rc
