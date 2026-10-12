"""Kill one cbench run (the harness python whose command line has --tag <tag>) and its child processes."""

import os
import sys

import psutil

tag = sys.argv[1]
me = os.getpid()
for p in psutil.process_iter(["pid", "name", "cmdline"]):
    try:
        cl = p.info["cmdline"] or []
        if p.info["pid"] == me or "cbench.py" not in " ".join(cl) or tag not in cl:
            continue
        kids = p.children(recursive=True)
        for k in kids + [p]:
            try:
                print("kill", k.pid, k.name())
                k.kill()
            except psutil.Error as e:
                print("failed", k.pid, e)
    except psutil.Error:
        pass
