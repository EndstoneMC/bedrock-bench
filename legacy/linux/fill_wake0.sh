#!/bin/bash
# After WAKE0_WIN_DONE: build cd97eea16 (lock 1), copy to wheelhouse, then wake0-100 (lock 2).
S="/c/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
L="$S/linux-bench"
W=endstone-0.11.14.dev10-cp314-cp314-manylinux_2_31_x86_64.whl
until [ -f "$S/WAKE0_WIN_DONE" ]; do sleep 20; done
echo "$(date +%T) WAKE0_WIN_DONE seen"
LOCK_LABEL=linux-wake0 bash "$L/locked.sh" "$L/build-wake0.log" bash fill_jobs.sh build-wake0
rc=$?
echo "$(date +%T) build-wake0 exit=$rc"
if [ $rc -ne 0 ] || [ ! -f "$S/dist-linux-wake0/$W" ]; then
    echo "BUILD FAILED"
    exit 1
fi
cp "$S/dist-linux-wake0/$W" "/d/Projects/Endstone/endstone/wheelhouse/$W" || { echo "COPY FAILED"; exit 1; }
echo "$(date +%T) wheel copied to wheelhouse"
LOCK_LABEL=linux-wake0 bash "$L/locked.sh" "$L/wake0-100.log" bash fill_jobs.sh wake0-100
echo "$(date +%T) wake0-100 exit=$?"
