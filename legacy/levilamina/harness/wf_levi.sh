#!/usr/bin/env bash
# 100-bot chain, one BENCH_LOCK per run: levi-opt-100, levi-100, levi-optall-100, vanilla-12651-100, then chaos on levi-opt.
S="C:/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
L="$S/levi"
H="$L/harness"
SV="$L/server"
VS="$L/vanilla-server"
CFG="$SV/plugins/LeviOptimize/config/config.json"
PARK="$L/parked"
MARK="$L/results/wf_levi.done"
COMMON=(--storm-prof 60 --steady-prof 0 --min-settle 300 --max-settle 900 --no-prof)
rm -f "$MARK"

restore() {
  if [ -d "$PARK/LeviOptimize" ] && [ ! -d "$SV/plugins/LeviOptimize" ]; then
    mv "$PARK/LeviOptimize" "$SV/plugins/LeviOptimize"
  fi
  cp "$L/configs/LeviOptimize.config.default.json" "$CFG"
  [ -f "$MARK" ] || echo "ended early $(date +%T)" > "$MARK"
}
trap restore EXIT
mkdir -p "$PARK"

cp "$L/configs/LeviOptimize.config.default.json" "$CFG"
bash "$H/run_one.sh" levi-opt-100 "$SV" bedrock_server_mod.exe "${COMMON[@]}"
echo "levi-opt-100 rc=$?"
sleep 20

mv "$SV/plugins/LeviOptimize" "$PARK/LeviOptimize"
bash "$H/run_one.sh" levi-100 "$SV" bedrock_server_mod.exe "${COMMON[@]}"
echo "levi-100 rc=$?"
mv "$PARK/LeviOptimize" "$SV/plugins/LeviOptimize"
sleep 20

cp "$L/configs/LeviOptimize.config.optall.json" "$CFG"
bash "$H/run_one.sh" levi-optall-100 "$SV" bedrock_server_mod.exe "${COMMON[@]}"
echo "levi-optall-100 rc=$?"
cp "$L/configs/LeviOptimize.config.default.json" "$CFG"
sleep 20

bash "$H/run_one.sh" vanilla-12651-100 "$VS" bedrock_server.exe "${COMMON[@]}"
echo "vanilla-12651-100 rc=$?"
sleep 20

bash "$H/run_one.sh" chaos-levi-opt "$SV" bedrock_server_mod.exe --storm-prof 0 --min-settle 120 --max-settle 300 --no-prof \
  --chaos 10
echo "chaos-levi-opt rc=$?"
echo "done $(date +%T)" > "$MARK"
