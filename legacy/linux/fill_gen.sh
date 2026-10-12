#!/bin/bash
# After head-100: wait for GEN_WHEEL_DONE (run gen-100) or GEN_FAILED (skip), then touch GEN_LINBENCH_DONE.
S="/c/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
L="$S/linux-bench"
until grep -q "job exit" "$L/head-100.log" 2>/dev/null; do sleep 20; done
until [ -f "$S/GEN_WHEEL_DONE" ] || [ -f "$S/GEN_FAILED" ]; do sleep 20; done
if [ -f "$S/GEN_FAILED" ]; then
    echo "$(date +%T) GEN_FAILED present, skipping gen-100"
else
    echo "$(date +%T) GEN_WHEEL_DONE: $(head -1 "$S/GEN_WHEEL_DONE")"
    LOCK_LABEL=linux-gen bash "$L/locked.sh" "$L/gen-100.log" bash fill_jobs.sh gen-100
    echo "$(date +%T) gen-100 exit=$?"
fi
touch "$S/GEN_LINBENCH_DONE"
echo "$(date +%T) touched GEN_LINBENCH_DONE"
