#!/bin/bash
# After LINUX_GO: build the commit it names (lock 1), copy to wheelhouse, then wake0-100 (lock 2).
S="/c/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
L="$S/linux-bench"
R=/d/Projects/Endstone/endstone
W=endstone-0.11.14.dev10-cp314-cp314-manylinux_2_31_x86_64.whl
until [ -f "$S/LINUX_GO" ]; do sleep 20; done
commit=$(grep -o -E '\b[0-9a-f]{7,40}\b' "$S/LINUX_GO" | head -1)
echo "$(date +%T) LINUX_GO seen: '$(tr -d '\r' < "$S/LINUX_GO" | head -3)' -> commit $commit"
[ -n "$commit" ] || { echo "NO COMMIT IN LINUX_GO"; exit 1; }
git -C "$R" cat-file -e "$commit^{commit}" 2>/dev/null || git -C "$R" fetch -q origin v0.11
git -C "$R" cat-file -e "$commit^{commit}" || { echo "COMMIT NOT FOUND $commit"; exit 1; }
echo "$(date +%T) building $(git -C "$R" log --oneline -1 "$commit")"
git -C "$R" merge-base --is-ancestor cd97eea16 "$commit" && echo "contains cd97eea16" || echo "WARNING: does not contain cd97eea16"
WAKE_COMMIT="$commit" LOCK_LABEL=linux-wake0 bash "$L/locked.sh" "$L/build-wake0.log" bash fill_jobs.sh build-wake0
rc=$?
echo "$(date +%T) build-wake0 exit=$rc"
if [ $rc -ne 0 ] || [ ! -f "$S/dist-linux-wake0/$W" ]; then
    echo "BUILD FAILED"
    exit 1
fi
cp "$S/dist-linux-wake0/$W" "$R/wheelhouse/$W" || { echo "COPY FAILED"; exit 1; }
echo "$(date +%T) wheel copied to wheelhouse"
LOCK_LABEL=linux-wake0 bash "$L/locked.sh" "$L/wake0-100.log" bash fill_jobs.sh wake0-100
echo "$(date +%T) wake0-100 exit=$?"
