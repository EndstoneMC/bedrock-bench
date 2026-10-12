#!/bin/bash
# Usage: locked.sh <log> bash fill_jobs.sh <job>   One server run or build per call, run from linux-bench.
S="/c/Users/Vincent/AppData/Local/Temp/claude/d--Projects-Endstone-endstone/7967334b-4dde-4367-ae32-73653b87ea94/scratchpad"
W14=wheels/after/endstone-0.11.14.dev4-cp314-cp314-manylinux_2_31_x86_64.whl
W13=wheels/pypi-01113/endstone-0.11.13-cp314-cp314-manylinux_2_31_x86_64.whl
PY="uv run --no-project python -I"
cd "$S/linux-bench" || exit 1
case "$1" in
chaos-after-r2) $PY chaos.py chaos-after-r2 --wheel "$W14" --chaos 15 ;;
vanilla-smoke) $PY smoke_vanilla.py ;;
vanilla-100) $PY bench.py vanilla-100 --vanilla ;;
pypi01113-100) $PY bench.py pypi01113-100 --wheel "$W13" ;;
chaos-vanilla) $PY chaos.py chaos-vanilla --vanilla --chaos 15 ;;
chaos-pypi01113) $PY chaos.py chaos-pypi01113 --wheel "$W13" --chaos 15 ;;
intest-after)
    (cd "$S/endstone-bench/gt" && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o "$S/linux-bench/loadbot-linux-intest" ./cmd/loadbot) || exit 3
    $PY intest_linux.py intest-after --wheel "$W14" ;;
intest-01113)
    [ -x "$S/linux-bench/loadbot-linux-intest" ] || [ -f "$S/linux-bench/loadbot-linux-intest" ] || exit 3
    $PY intest_linux.py intest-01113 --wheel "$W13" ;;
build-verify)
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\Users\Vincent\AppData\Local\Temp\claude\d--Projects-Endstone-endstone\7967334b-4dde-4367-ae32-73653b87ea94\scratchpad\linux-bench\build_verify.ps1' ;;
chaos-verify)
    w=$(ls wheels/verify/endstone-*.whl | head -1)
    [ -n "$w" ] || exit 4
    $PY chaos.py chaos-verify --wheel "$w" --chaos 15 ;;
head-100) $PY bench.py head-100 --wheel D:/Projects/Endstone/endstone/wheelhouse/endstone-0.11.14.dev8-cp314-cp314-manylinux_2_31_x86_64.whl ;;
gen-100)
    w=$(head -1 "$S/GEN_WHEEL_DONE" | tr -d '\r' | tr '\134' '/')
    [ -f "$w" ] || { echo "gen wheel not found: $w"; exit 5; }
    $PY bench.py gen-100 --wheel "$w" ;;
build-wake0)
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\Users\Vincent\AppData\Local\Temp\claude\d--Projects-Endstone-endstone\7967334b-4dde-4367-ae32-73653b87ea94\scratchpad\linux-bench\build_wheel.ps1' -Commit "${WAKE_COMMIT:?}" -Version 0.11.14.dev10 -Name wake0 ;;
wake0-100) $PY bench.py wake0-100 --wheel D:/Projects/Endstone/endstone/wheelhouse/endstone-0.11.14.dev10-cp314-cp314-manylinux_2_31_x86_64.whl ;;
build-wake01)
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\Users\Vincent\AppData\Local\Temp\claude\d--Projects-Endstone-endstone\7967334b-4dde-4367-ae32-73653b87ea94\scratchpad\linux-bench\build_wheel.ps1' -Commit fdea21001 -Version 0.11.14.dev12 -Name wake01 -CopyFile src/endstone/runtime/bedrock_hooks/server_instance.cpp -ExpectInDiff 'min_backlog_duration_seconds_ = 0.01F' ;;
wake01-100) $PY bench.py wake01-100 --wheel D:/Projects/Endstone/endstone/wheelhouse/endstone-0.11.14.dev12-cp314-cp314-manylinux_2_31_x86_64.whl ;;
build-final)
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\Users\Vincent\AppData\Local\Temp\claude\d--Projects-Endstone-endstone\7967334b-4dde-4367-ae32-73653b87ea94\scratchpad\linux-bench\build_wheel.ps1' -Commit "${FINAL_COMMIT:?}" -Version 0.11.14.dev13 -Name final ;;
intest-final)
    [ -f "$S/linux-bench/loadbot-linux-intest" ] || exit 3
    $PY intest_linux.py intest-final --wheel D:/Projects/Endstone/endstone/wheelhouse/endstone-0.11.14.dev13-cp314-cp314-manylinux_2_31_x86_64.whl ;;
*) echo "unknown job $1"; exit 9 ;;
esac
