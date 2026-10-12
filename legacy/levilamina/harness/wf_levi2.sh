#!/usr/bin/env bash
# After wf_levi.sh: rerun levi-100 (LeviOptimize parked) as levi-100b; its first run overlapped a Minecraft client.
S="C:/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
L="$S/levi"
SV="$L/server"
PARK="$L/parked"
MARK="$L/results/wf_levi2.done"
rm -f "$MARK"

restore() {
  if [ -d "$PARK/LeviOptimize" ] && [ ! -d "$SV/plugins/LeviOptimize" ]; then
    mv "$PARK/LeviOptimize" "$SV/plugins/LeviOptimize"
  fi
  [ -f "$MARK" ] || echo "ended early $(date +%T)" > "$MARK"
}
trap restore EXIT

until [ -f "$L/results/wf_levi.done" ]; do sleep 20; done
echo "wf_levi.done seen $(date +%T): $(cat "$L/results/wf_levi.done")"
sleep 20
mkdir -p "$PARK"
mv "$SV/plugins/LeviOptimize" "$PARK/LeviOptimize"
bash "$L/harness/run_one.sh" levi-100b "$SV" bedrock_server_mod.exe \
  --storm-prof 60 --steady-prof 0 --min-settle 300 --max-settle 900 --no-prof
echo "levi-100b rc=$?"
mv "$PARK/LeviOptimize" "$SV/plugins/LeviOptimize"
echo "done $(date +%T)" > "$MARK"
