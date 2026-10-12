#!/usr/bin/env bash
# Pre-generate Nukkit's world after the vanilla run, then run the measured Nukkit benchmark once BUILD15_DONE exists.
S=/c/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad
C=$S/custom-bench
until tr -d '\000' < "$S/endstone-bench/results/vanilla-100.out" 2>/dev/null | grep -q "restored\|server exit"; do sleep 15; done
echo "$(date +%T) vanilla done"
cd "$C/build/gtfork" && GOFLAGS=-mod=mod go build -o "$C/build/loadbot-2193x.exe" ./cmd/loadbot && echo "$(date +%T) bot rebuilt"
cd "$C/scripts" && uv run --no-project --with psutil python -I pregen.py 12 64 > "$C/results/nukkit-pregen.out" 2>&1
echo "$(date +%T) pregen finished: $(grep 'PREGEN RESULT' "$C/results/nukkit-pregen.out")"
until [ -f "$S/BUILD15_DONE" ]; do sleep 15; done
echo "$(date +%T) BUILD15_DONE seen, starting measured run"
cd "$C/scripts" && uv run --no-project --with psutil python -I cbench.py --server nukkit --tag nukkit-100-pregen \
  --world-template "$(cygpath -w "$C/servers/nukkit-template")" > "$C/results/nukkit-100-pregen.out" 2>&1
echo "$(date +%T) measured run finished"
