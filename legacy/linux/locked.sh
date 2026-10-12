#!/bin/bash
# Usage: locked.sh <log> <cmd...>  Run one job in linux-bench while holding S/BENCH_LOCK.
S="/c/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
LOCK="$S/BENCH_LOCK"
log="$1"
shift
cd "$S/linux-bench" || exit 1
[ -f "$S/custom-bench/RERUN_DONE" ] || { echo "RERUN_DONE missing" >> "$log"; exit 2; }
label="${LOCK_LABEL:-linux-fill}"
until (set -o noclobber; echo $label $(date +%T) > "$LOCK") 2>/dev/null; do sleep 15; done
trap 'grep -q "^$label " "$LOCK" 2>/dev/null && rm -f "$LOCK"' EXIT
echo "$(date +%T) lock acquired ($label): $*" >> "$log"
"$@" >> "$log" 2>&1
rc=$?
echo "$(date +%T) job exit=$rc" >> "$log"
exit $rc
