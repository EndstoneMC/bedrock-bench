"""Pre-generate a server's benchmark world (every harness grid cell and the spawn, radius 12 chunks) as a template.

  python -I pregen.py [radius] [in-flight] [nukkit|pnx]
"""

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

CB = Path(__file__).resolve().parent.parent
SERVER = sys.argv[3] if len(sys.argv) > 3 else "nukkit"
SRV = CB / "servers" / SERVER
TEMPLATE = CB / "servers" / f"{SERVER}-template"
JAVA = r"C:\Program Files\Java\jdk-21\bin\java.exe"
JAR = {"nukkit": "nukkit.jar", "pnx": "powernukkitx.jar"}[SERVER]
OUT = CB / "results" / f"{SERVER}-pregen"


def main():
    radius = sys.argv[1] if len(sys.argv) > 1 else "12"
    inflight = sys.argv[2] if len(sys.argv) > 2 else "64"
    for d in ("worlds", "players"):
        shutil.rmtree(SRV / d, ignore_errors=True)
    if SERVER == "pnx":
        tpl = json.loads((CB / "scripts" / "pnx-world-config.json").read_text())
        for g in tpl["generators"].values():
            g["seed"] = 20261010
        (SRV / "worlds" / "bench").mkdir(parents=True)
        (SRV / "worlds" / "bench" / "config.json").write_text(json.dumps(tpl, indent=2))
    OUT.mkdir(parents=True, exist_ok=True)
    log = open(OUT / "server.log", "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen([JAVA, "-jar", str(SRV / JAR)], cwd=SRV, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
    psutil.Process(p.pid).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    ready, done = threading.Event(), threading.Event()
    result = {}

    def pump():
        for line in p.stdout:
            line = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line)
            log.write(f"{time.strftime('%H:%M:%S')} {line}")
            log.flush()
            if "Done (" in line:
                ready.set()
            m = re.search(r"PREGEN DONE (\d+) chunks in ([\d.]+) s", line)
            if m:
                result.update(chunks=int(m.group(1)), seconds=float(m.group(2)))
                done.set()
            if "PREGEN progress" in line or "PREGEN start" in line:
                print(line.strip(), flush=True)

    threading.Thread(target=pump, daemon=True).start()
    if not ready.wait(300):
        raise SystemExit("nukkit did not start")
    t0 = time.time()
    p.stdin.write(f"pregen {radius} {inflight}\n")
    p.stdin.flush()
    ok = done.wait(3600)
    result.update(ok=ok, wall_s=round(time.time() - t0, 1), radius=int(radius), in_flight=int(inflight))
    p.stdin.write("stop\n")
    p.stdin.flush()
    p.wait(300)
    result["exit"] = p.returncode
    if ok:
        shutil.rmtree(TEMPLATE, ignore_errors=True)
        TEMPLATE.mkdir(parents=True)
        shutil.copytree(SRV / "worlds", TEMPLATE / "worlds")
        result["template_mb"] = round(sum(f.stat().st_size for f in (TEMPLATE / "worlds").rglob("*") if f.is_file()) / 2 ** 20)
    (OUT / "pregen.json").write_text(json.dumps(result, indent=1))
    print("PREGEN RESULT", json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
