"""Probe the surface at every bot cell centre in a Dragonfly world copy: PNX terrain vs Dragonfly's flat fallback.

  python -I dfprobe.py <world-dir>
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

CB = Path(__file__).resolve().parent.parent
EXE = CB / "build" / "dfbench" / "dfbench.exe"


def main():
    src = Path(sys.argv[1])
    tmp = Path(tempfile.mkdtemp(dir=CB / "servers"))
    shutil.copytree(src, tmp / "world")
    shutil.copy(CB / "servers" / "dragonfly" / "config.toml", tmp / "config.toml")
    p = subprocess.Popen([str(EXE)], cwd=tmp, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    rows, ready = {}, threading.Event()

    def pump():
        for line in p.stdout:
            if "Server started" in line:
                ready.set()
            m = re.search(r"HEIGHT (-?\d+) (-?\d+) y=(-?\d+) block=(\S+)", line)
            if m:
                rows[(int(m.group(1)), int(m.group(2)))] = (int(m.group(3)), m.group(4))

    threading.Thread(target=pump, daemon=True).start()
    ready.wait(60)
    cells = [(1280 + (i % 11) * 640, 1280 + (i // 11) * 640) for i in range(100)]
    for x, z in cells:
        p.stdin.write(f"height {x} {z}\n")
        p.stdin.flush()
    end = time.time() + 120
    while len(rows) < len(cells) and time.time() < end:
        time.sleep(0.5)
    p.stdin.write("stop\n")
    p.stdin.flush()
    p.wait(60)
    flat = [c for c in cells if rows.get(c) == (-61, "minecraft:grass_block")]
    out = {"probed": len(rows), "flat_fallback_cells": len(flat), "terrain_cells": len(rows) - len(flat),
           "flat_cells": [cells.index(c) for c in flat]}
    print(json.dumps(out))
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
