#!/usr/bin/env bash
# Waits for RERUN_DONE, then runs the three windows-fill jobs, one lock per job.
S="C:/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
until [ -f "$S/custom-bench/RERUN_DONE" ]; do sleep 20; done
echo "RERUN_DONE seen $(date +%T)"

bash "$S/wf_run.sh" chaos-01113-win --arm dll-pypi --storm-prof 0 --min-settle 120 --max-settle 300 --no-prof --chaos 10 \
  > "$S/wf_run-chaos-01113-win.log" 2>&1
echo "chaos-01113-win rc=$? $(date +%T)"
sleep 20

bash "$S/wf_run.sh" chaos-vanilla-win --vanilla --storm-prof 0 --min-settle 120 --max-settle 300 --no-prof --chaos 10 \
  > "$S/wf_run-chaos-vanilla-win.log" 2>&1
echo "chaos-vanilla-win rc=$? $(date +%T)"
sleep 20

bash "$S/wf_run.sh" intest-01113 --arm dll-pypi --intest > "$S/wf_run-intest-01113.log" 2>&1
echo "intest-01113 rc=$? $(date +%T)"
