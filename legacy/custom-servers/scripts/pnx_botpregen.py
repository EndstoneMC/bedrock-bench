"""Pre-generate PNX's benchmark world through its own player chunk pipeline: a few bots visit every harness grid cell
with the view distance raised to 12, then the server saves on stop and the world is kept as a template.

  python -I pnx_botpregen.py [bots]
"""

import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cbench as cb  # noqa: E402
import harness as h  # noqa: E402
import psutil  # noqa: E402
from chaos import ChaosBots  # noqa: E402

CB = cb.CB
SRV = CB / "servers" / "pnx"
TEMPLATE = CB / "servers" / "pnx-template"
OUT = CB / "results" / "pnx-pregen"
CELLS = 121


def set_view(n):
    p = SRV / "pnx.yml"
    p.write_text(re.sub(r"(?m)^  viewDistance: \d+", f"  viewDistance: {n}", p.read_text()))


def main():
    nb = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    fill = len(sys.argv) > 2 and sys.argv[2] == "fill"
    cells_n = CELLS if not fill else 100
    min_dwell, still_need, rate = (40, 2, 5) if not fill else (60, 3, 2)
    out = OUT if not fill else CB / "results" / "pnx-pregen-fill"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    ad = cb.PNX()
    h.PORT = ad.port
    h.E_CORES = list(range(16, 28))
    ad.prepare()
    if fill:
        shutil.copytree(TEMPLATE / "worlds", SRV / "worlds", dirs_exist_ok=True)
    (SRV / "plugins" / "PnxPreGen.jar").unlink(missing_ok=True)
    set_view(12)
    res = {"bots": nb, "view_distance": 12, "fill_pass": fill, "rounds": []}
    t0 = time.time()
    srv = cb.Server(ad, out / "server.log", list(range(16)), psutil.NORMAL_PRIORITY_CLASS)
    try:
        if not srv.started.wait(300):
            raise RuntimeError("pnx did not start")
        b = ChaosBots(ad.bot, nb, "g", "7200s", out / "bots.log", srv, 0, extra=["-csg", *ad.bot_extra])
        if not b.allj.wait(300):
            raise RuntimeError("bots did not join")
        time.sleep(5)

        def uniq():
            m = re.search(r"uniq=(\d+)", b.stats[-1][1]) if b.stats else None
            return int(m.group(1)) if m else 0

        for r in range((cells_n + nb - 1) // nb):
            cells = list(range(r * nb, min(cells_n, (r + 1) * nb)))
            for j, c in enumerate(cells):
                x, z = h.cell(c)
                srv.cmd(f"tp g{j:02d} {x} 300 {z}")
            ts, start, last, still = time.time(), uniq(), uniq(), 0
            while time.time() - ts < 240:
                time.sleep(10)
                u = uniq()
                still = still + 1 if u - last < rate * 10 else 0
                last = u
                if time.time() - ts > min_dwell and still >= still_need:
                    break
            row = {"round": r, "cells": cells, "uniq_gain": last - start, "s": round(time.time() - ts)}
            res["rounds"].append(row)
            print(time.strftime("%H:%M:%S"), "round", row, "total uniq", last, flush=True)
        res["bot_uniq_total"] = uniq()
        b.stop()
    finally:
        srv.stop()
        set_view(10)
    res["seconds"] = round(time.time() - t0, 1)
    shutil.rmtree(TEMPLATE, ignore_errors=True)
    TEMPLATE.mkdir(parents=True)
    shutil.copytree(SRV / "worlds", TEMPLATE / "worlds")
    res["template_mb"] = round(sum(f.stat().st_size for f in (TEMPLATE / "worlds").rglob("*") if f.is_file()) / 2 ** 20)
    res["ok"] = True
    (out / "pregen.json").write_text(json.dumps(res, indent=1))
    print("PREGEN RESULT", json.dumps({k: v for k, v in res.items() if k != "rounds"}), flush=True)


if __name__ == "__main__":
    main()
