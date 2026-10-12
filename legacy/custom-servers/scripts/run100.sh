#!/usr/bin/env bash
# Wait for the lead's Endstone runs to finish, then run one server's 100-bot benchmark.
S=/c/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad
C=$S/custom-bench
srv=$1; tag=$2; shift 2
while true; do
  if [ -f "$S/endstone-bench/results/ENDSTONE_DONE" ] && ! tasklist | grep -qiE "^(bedrock_server|loadbot)\.exe|^loadbot-chaos\.exe"; then break; fi
  sleep 20
done
echo "$(date +%T) start $srv $tag"
cd "$C/scripts" && uv run --no-project --with psutil python -I cbench.py --server "$srv" --tag "$tag" "$@" > "$C/results/$tag.out" 2>&1
echo "$(date +%T) done $srv $tag rc=$?"
