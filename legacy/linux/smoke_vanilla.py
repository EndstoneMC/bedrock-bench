"""Start vanilla BDS the way bench.py --vanilla does, check its console reaches us and answers commands, then stop.

Usage: uv run --no-project python -I smoke_vanilla.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench  # noqa: E402

name = "es-bench-vanilla-smoke"
out = bench.B / "results" / "vanilla-smoke"
out.mkdir(parents=True, exist_ok=True)
bench.OUT = out
image, _, _ = bench.build_image(bench.VANILLA_IMAGE_WHEEL)
bench.run(["docker", "rm", "-f", name], check=False)
bench.prepare_volumes(image, "1.26.52.3", name, dict(bench.PROPS))
srv = bench.Server(name, image, name, "0-15", "default", out / "server.log", vanilla=True)
try:
    for _ in range(180):
        if srv.started.wait(1) or srv.p.poll() is not None:
            break
    bench.log("started", srv.started.is_set(), srv.grep(r"Version: [\d.]+"), srv.grep(r"Endstone.*"))
    if not srv.started.is_set():
        raise SystemExit("vanilla did not start")
    for _ in range(3):
        srv.cmd("time query gametime")
        time.sleep(2)
    srv.cmd("status")
    time.sleep(2)
    bench.log("gametime", srv.gametime[-3:], "info", srv.info())
    bench.log("snapshot threads", sorted({n for _, n in srv.snapshot()["threads"].values()}))
    bench.log("status reply", srv.grep(r"(?i)unknown command.*status.*|Server status.*"))
    with srv.lock:
        bench.log("first lines", [ln.rstrip() for ln in srv.lines[:3]])
finally:
    srv.stop()
    bench.log("exit", srv.p.returncode)
    bench.run(["docker", "rm", "-f", name], check=False)
    bench.run(["docker", "volume", "rm", "-f", name], check=False)
