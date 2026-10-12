"""Start a server folder once with the bench properties, no bots: generates mod configs, shows the console format.

  uv run --no-project --with psutil python -I firststart.py <server-dir> <exe> <log>
"""

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_levi as rl  # noqa: E402

server, exe, logp = Path(sys.argv[1]).resolve(), sys.argv[2], Path(sys.argv[3])
rl.SERVER, rl.EXE = server, exe
props = server / "server.properties"
orig = props.read_text(encoding="utf-8")
text = orig
for k, v in rl.TARGET.items():
    text, n = re.subn(rf"(?m)^{re.escape(k)}=.*$", lambda _m: f"{k}={v}", text)
    if not n:
        text += f"\n{k}={v}\n"
props.write_text(text, encoding="utf-8")
try:
    srv = rl.Server(logp, vanilla=True)
    ok = srv.started.wait(300)
    print("started", ok, flush=True)
    bp = srv.find_bds(10)
    print("pid", bp.pid, "children", [(c.pid, c.name()) for c in bp.children(recursive=True)], flush=True)
    for _ in range(3):
        srv.cmd("time query gametime")
        time.sleep(2)
    print("gametime", srv.gametime, flush=True)
    srv.cmd("version")
    time.sleep(3)
    srv.stop()
    print("exit", srv.p.returncode, flush=True)
finally:
    props.write_text(orig, encoding="utf-8")
