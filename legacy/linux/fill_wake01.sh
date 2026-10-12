#!/bin/bash
# After WAKE01_WIN_DONE: build fdea21001 + the uncommitted 0.01 s threshold (lock 1), copy to wheelhouse, then wake01-100 (lock 2).
S="/c/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
L="$S/linux-bench"
R=/d/Projects/Endstone/endstone
W=endstone-0.11.14.dev12-cp314-cp314-manylinux_2_31_x86_64.whl
until [ -f "$S/WAKE01_WIN_DONE" ]; do sleep 20; done
echo "$(date +%T) WAKE01_WIN_DONE seen"
LOCK_LABEL=linux-wake01 bash "$L/locked.sh" "$L/build-wake01.log" bash fill_jobs.sh build-wake01
rc=$?
echo "$(date +%T) build-wake01 exit=$rc"
if [ $rc -ne 0 ] || [ ! -f "$S/dist-linux-wake01/$W" ]; then
    echo "BUILD FAILED"
    exit 1
fi
cp "$S/dist-linux-wake01/$W" "$R/wheelhouse/$W" || { echo "COPY FAILED"; exit 1; }
echo "$(date +%T) wheel copied to wheelhouse"
LOCK_LABEL=linux-wake01 bash "$L/locked.sh" "$L/wake01-100.log" bash fill_jobs.sh wake01-100
echo "$(date +%T) wake01-100 exit=$?"
