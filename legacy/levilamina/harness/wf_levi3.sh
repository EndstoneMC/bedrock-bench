#!/usr/bin/env bash
# After wf_levi2.sh: second samples of vanilla 1.26.51 and LeviLamina+LeviOptimize (defaults) for run-to-run noise.
S="C:/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
L="$S/levi"
MARK="$L/results/wf_levi3.done"
COMMON=(--storm-prof 60 --steady-prof 0 --min-settle 300 --max-settle 900 --no-prof)
rm -f "$MARK"
trap '[ -f "$MARK" ] || echo "ended early $(date +%T)" > "$MARK"' EXIT

until [ -f "$L/results/wf_levi2.done" ]; do sleep 20; done
echo "wf_levi2.done seen $(date +%T)"
sleep 20
bash "$L/harness/run_one.sh" vanilla-12651-100b "$L/vanilla-server" bedrock_server.exe "${COMMON[@]}"
echo "vanilla-12651-100b rc=$?"
sleep 20
cp "$L/configs/LeviOptimize.config.default.json" "$L/server/plugins/LeviOptimize/config/config.json"
bash "$L/harness/run_one.sh" levi-opt-100b "$L/server" bedrock_server_mod.exe "${COMMON[@]}"
echo "levi-opt-100b rc=$?"
echo "done $(date +%T)" > "$MARK"
