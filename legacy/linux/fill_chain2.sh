#!/bin/bash
# Remaining Linux fill jobs, one lock each, none before NET_DONE and the running chaos-pypi01113 (locked.sh pid $1).
S="/c/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
L="$S/linux-bench"
running="$1"
gate() {
    until [ -f "$S/NET_DONE" ] && { [ -z "$running" ] || [ ! -d "/proc/$running" ]; }; do sleep 20; done
}
job() {
    gate
    echo "$(date +%T) start $1"
    bash "$L/locked.sh" "$L/$1.log" bash fill_jobs.sh "$1"
    echo "$(date +%T) $1 exit=$?"
}

gate
if ! grep -q "CHAOS {" "$L/chaos-pypi01113.log" 2>/dev/null; then
    mv "$L/chaos-pypi01113.log" "$L/chaos-pypi01113.aborted.log" 2>/dev/null
    job chaos-pypi01113
fi
job intest-after
job build-verify
w=$(ls "$S"/dist-linux-verify/endstone-*.whl 2>/dev/null | head -1)
if [ -z "$w" ]; then echo "no verify wheel"; exit 1; fi
mkdir -p "$L/wheels/verify" && cp "$w" "$L/wheels/verify/"
if ! uv run --no-project python -I -c "
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
so = next(n for n in z.namelist() if n.endswith('libendstone_runtime.so'))
sys.exit(0 if b'[verify]' in z.read(so) else 1)
" "$w"; then
    echo "verify wheel lacks [verify] strings"
    exit 1
fi
echo "$(date +%T) verify wheel ok: $w"
job chaos-verify
