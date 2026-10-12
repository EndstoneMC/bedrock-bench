#!/bin/bash
# Final Linux validation: after WAKE01_WIN_DONE, build origin/v0.11 HEAD clean (lock 1), copy to wheelhouse, then intest (lock 2).
S="/c/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
L="$S/linux-bench"
R=/d/Projects/Endstone/endstone
W=endstone-0.11.14.dev13-cp314-cp314-manylinux_2_31_x86_64.whl
until [ -f "$S/WAKE01_WIN_DONE" ]; do sleep 20; done
echo "$(date +%T) WAKE01_WIN_DONE seen"
head=$(git -C "$R" ls-remote origin refs/heads/v0.11 | cut -c1-40)
echo "$(date +%T) origin/v0.11 = $head"
[ -n "$head" ] || { echo "LS-REMOTE FAILED"; exit 1; }
if [ "${head:0:9}" != "fdea21001" ]; then
    echo "HEAD MOVED from fdea21001 to $head"
    git -C "$R" cat-file -e "$head^{commit}" 2>/dev/null || git -C "$R" fetch -q origin v0.11
fi
git -C "$R" cat-file -e "$head^{commit}" || { echo "COMMIT NOT FOUND $head"; exit 1; }
echo "$(date +%T) building $(git -C "$R" log --oneline -1 "$head")"
FINAL_COMMIT="$head" LOCK_LABEL=linux-final bash "$L/locked.sh" "$L/build-final.log" bash fill_jobs.sh build-final
rc=$?
echo "$(date +%T) build-final exit=$rc"
if [ $rc -ne 0 ] || [ ! -f "$S/dist-linux-final/$W" ]; then
    echo "BUILD FAILED"
    exit 1
fi
cp "$S/dist-linux-final/$W" "$R/wheelhouse/$W" || { echo "COPY FAILED"; exit 1; }
echo "$(date +%T) wheel copied to wheelhouse"
LOCK_LABEL=linux-final bash "$L/locked.sh" "$L/intest-final.log" bash fill_jobs.sh intest-final
echo "$(date +%T) intest-final exit=$?"
