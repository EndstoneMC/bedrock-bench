"""Start a server once, wait for a ready line (or a timeout), send console lines, then stop it.

  python -I firstrun.py <cwd> <log> <ready-regex> <timeout-s> <stop-cmd> [--send cmd]... -- <argv...>
"""

import re
import subprocess
import sys
import threading
import time

import psutil


def main():
    a = sys.argv[1:]
    i = a.index("--")
    head, argv = a[:i], a[i + 1:]
    cwd, logp, ready, timeout, stop = head[:5]
    sends = [head[j + 1] for j in range(5, len(head)) if head[j] == "--send"]
    pre = [head[j + 1] for j in range(5, len(head)) if head[j] == "--pre"]
    log = open(logp, "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", bufsize=1)
    try:
        ps = psutil.Process(p.pid)
        ps.cpu_affinity(list(range(20, 28)))
        ps.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except psutil.Error:
        pass
    ok = threading.Event()
    for s in pre:
        p.stdin.write(s + "\n")
        p.stdin.flush()

    def pump():
        for line in p.stdout:
            log.write(f"{time.strftime('%H:%M:%S')} {line}")
            log.flush()
            if re.search(ready, line):
                ok.set()

    threading.Thread(target=pump, daemon=True).start()
    t0 = time.time()
    ok.wait(float(timeout))
    print("ready" if ok.is_set() else "timeout", round(time.time() - t0, 1), flush=True)
    for s in sends:
        p.stdin.write(s + "\n")
        p.stdin.flush()
        time.sleep(2)
    time.sleep(2)
    try:
        p.stdin.write(stop + "\n")
        p.stdin.flush()
        p.wait(90)
    except Exception as e:
        print("stop failed", repr(e), "killing our process", flush=True)
        for c in psutil.Process(p.pid).children(recursive=True):
            c.kill()
        p.kill()
    print("exit", p.returncode, flush=True)


if __name__ == "__main__":
    main()
