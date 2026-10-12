#!/usr/bin/env bash
# usage: wf_run.sh <tag> <run_endstone.py args...>
# Takes BENCH_LOCK, runs one harness run, releases the lock.
S="C:/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
B="$S/endstone-bench"
TAG="$1"; shift
LOCK="$S/BENCH_LOCK"

release() {
  if [ -f "$LOCK" ] && [[ "$(cat "$LOCK" 2>/dev/null)" == windows-fill* ]]; then
    rm -f "$LOCK"
    echo "lock released $(date +%T)"
  fi
}

while ! (set -o noclobber; echo "windows-fill $(date +%T)" > "$LOCK") 2>/dev/null; do
  echo "lock held by: $(cat "$LOCK" 2>/dev/null) at $(date +%T); retrying"
  sleep 15
done
trap release EXIT
echo "lock acquired $(date +%T): $(cat "$LOCK")"

cd "$B" || exit 1
uv run --no-project --with psutil python -I run_endstone.py --tag "$TAG" "$@" > "$B/results/$TAG.out" 2>&1
rc=$?
echo "exit=$rc" >> "$B/results/$TAG.out"
echo "harness exit $rc at $(date +%T)"
release
trap - EXIT
exit $rc
