#!/bin/bash
# After both intests: rebuild the verify wheel (dev6, macro via tools.build:defines), check it, then the verify chaos.
S="/c/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
L="$S/linux-bench"
job() {
    until [ -f "$S/NET_DONE" ]; do sleep 20; done
    echo "$(date +%T) start $1"
    bash "$L/locked.sh" "$L/$1.log" bash fill_jobs.sh "$1"
    echo "$(date +%T) $1 exit=$?"
}

until grep -q "job exit" "$L/intest-01113.log" 2>/dev/null; do sleep 20; done
[ -f "$L/build-verify.log" ] && mv "$L/build-verify.log" "$L/build-verify.dev5.log"
rm -f "$L"/wheels/verify/*.whl
job build-verify
w=$(ls "$S"/dist-linux-verify/endstone-0.11.14.dev6-*.whl 2>/dev/null | head -1)
if [ -z "$w" ]; then echo "no verify wheel"; exit 1; fi
if ! uv run --no-project python -I -c "
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
so = next(n for n in z.namelist() if n.endswith('libendstone_runtime.so'))
sys.exit(0 if b'[verify]' in z.read(so) else 1)
" "$w"; then
    echo "verify wheel lacks [verify] strings"
    exit 1
fi
mkdir -p "$L/wheels/verify" && cp "$w" "$L/wheels/verify/"
echo "$(date +%T) verify wheel ok: $w"
[ -f "$L/chaos-verify.log" ] && mv "$L/chaos-verify.log" "$L/chaos-verify.skipped.log"
job chaos-verify
