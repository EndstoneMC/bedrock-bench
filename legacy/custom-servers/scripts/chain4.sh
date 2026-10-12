#!/usr/bin/env bash
# PNX world pre-generation, the PNX run on it, then Dragonfly on the PNX world; each job holds S/BENCH_LOCK.
S=/c/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad
C=$S/custom-bench
R=$C/results
PY="uv run --no-project --with psutil python -I"

lock() {
  until (set -o noclobber; echo "custom-bench $(date +%T) $1" > "$S/BENCH_LOCK") 2>/dev/null; do sleep 15; done
  echo "$(date +%T) lock $1"
}
unlock() {
  grep -q "^custom-bench" "$S/BENCH_LOCK" 2>/dev/null && rm -f "$S/BENCH_LOCK"
  echo "$(date +%T) unlock"
}

cd "$C/scripts" || exit 1

grep -q "^custom-bench" "$S/BENCH_LOCK" 2>/dev/null && echo "$(date +%T) keeping our lock" || lock pnx-pregen
until grep -q "PREGEN RESULT\|Traceback" "$R/pnx-pregen.out"; do sleep 10; done
$PY pnx_botpregen.py 10 fill > "$R/pnx-pregen-fill.out" 2>&1
echo "$(date +%T) fill pass: $(grep 'PREGEN RESULT' "$R/pnx-pregen-fill.out")"
echo "$(date +%T) $(grep 'PREGEN RESULT' "$R/pnx-pregen.out")"
unlock
grep -q '"ok": true' "$R/pnx-pregen-fill.out" || { echo "pnx pregen failed"; exit 1; }

lock pnx-run
$PY cbench.py --server pnx --tag pnx-100-pregen --world-template "$(cygpath -w "$C/servers/pnx-template")" > "$R/pnx-100-pregen.out" 2>&1
echo "$(date +%T) pnx run finished"
unlock

lock dragonfly
T=$C/servers/dragonfly-pnxworld
rm -rf "$T" && mkdir -p "$T/world" && cp -r "$C/servers/pnx-template/worlds/bench/db" "$T/world/db" \
  && cp "$C/servers/dragonfly-leveldat/level.dat" "$T/world/level.dat"
rm -rf "$C/servers/dragonfly/world" && cp -r "$T/world" "$C/servers/dragonfly/world"
SD="$(cygpath -w "$C/servers/dragonfly")"
timeout 120 $PY firstrun.py "$SD" "$SD\\dftest-pnx.log" "Server started" 60 stop --send "height 1280 1280" \
  --send "height 1920 1280" --send "height 7680 5120" -- "$(cygpath -w "$C/build/dfbench/dfbench.exe")" > /dev/null 2>&1
cp "$C/servers/dragonfly/dftest-pnx.log" "$R/dragonfly-pnxworld-loadtest.log"
if grep -q "ERROR load chunk\|panic" "$R/dragonfly-pnxworld-loadtest.log" || ! grep "HEIGHT" "$R/dragonfly-pnxworld-loadtest.log" | grep -vq "block=minecraft:air"; then
  echo "$(date +%T) dragonfly cannot load the PNX world (see dragonfly-pnxworld-loadtest.log)"
else
  $PY cbench.py --server dragonfly --tag dragonfly-100-pnxworld --world-template "$(cygpath -w "$T")" > "$R/dragonfly-100-pnxworld.out" 2>&1
  echo "$(date +%T) dragonfly run finished"
fi
unlock
