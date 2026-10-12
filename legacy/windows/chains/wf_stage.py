"""Stage the network build's endstone_runtime.dll (+ .pdb) into endstone-bench/dll-net from the path in NETBUILD_DONE.

  python -I wf_stage.py <scratchpad> <min-mtime-epoch>
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

S = Path(sys.argv[1])
MIN_MTIME = float(sys.argv[2])
MARKER = sys.argv[3] if len(sys.argv) > 3 else "NETBUILD_DONE"
ARM = S / "endstone-bench" / (sys.argv[4] if len(sys.argv) > 4 else "dll-net")
REPO = Path("D:/Projects/Endstone/endstone")
FALLBACK = REPO / "build" / "RelWithDebInfo" / "src" / "endstone" / "runtime" / "endstone_runtime.dll"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


text = (S / MARKER).read_text(encoding="utf-8", errors="replace")
print(MARKER + ":", text.strip()[:2000])
cands = []
for tok in re.findall(r"[A-Za-z]:[\\/][^\s\"'<>|*?]+|/[A-Za-z]/[^\s\"'<>|*?]+", text):
    tok = tok.rstrip(".,;:)")
    if re.match(r"^/[A-Za-z]/", tok):
        tok = tok[1] + ":" + tok[2:]
    p = Path(tok)
    if p.exists():
        cands.append(p)
print("paths found:", [str(c) for c in cands])

ARM.mkdir(parents=True, exist_ok=True)
for f in ARM.iterdir():
    shutil.rmtree(f) if f.is_dir() else f.unlink()

src = None
for c in cands:
    if c.is_file() and c.name.lower() == "endstone_runtime.dll":
        src = c
        break
if src is None:
    for c in cands:
        if c.is_dir():
            dlls = sorted(c.rglob("endstone_runtime.dll"), key=lambda p: p.stat().st_mtime, reverse=True)
            if dlls:
                src = dlls[0]
                break
whl = next((c for c in cands if c.is_file() and c.suffix.lower() == ".whl"), None)
if src is None and whl is not None:
    with zipfile.ZipFile(whl) as z:
        for n in z.namelist():
            base = n.rsplit("/", 1)[-1]
            if base in ("endstone_runtime.dll", "endstone_runtime.pdb"):
                (ARM / base).write_bytes(z.read(n))
    src = whl
    print("extracted from wheel", whl, [f.name for f in ARM.iterdir()])
if src is None:
    src = FALLBACK
    print("no usable path in " + MARKER + ", falling back to", src)

if src.suffix.lower() == ".dll":
    if src.stat().st_mtime < MIN_MTIME:
        sys.exit(f"STAGE FAILED: {src} is older than the expected build ({time.ctime(src.stat().st_mtime)})")
    shutil.copyfile(src, ARM / "endstone_runtime.dll")
    pdb = src.with_suffix(".pdb")
    if pdb.exists():
        shutil.copyfile(pdb, ARM / "endstone_runtime.pdb")
if not (ARM / "endstone_runtime.dll").exists():
    sys.exit("STAGE FAILED: no endstone_runtime.dll staged")

head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short=9", "HEAD"], capture_output=True, text=True).stdout.strip()
status = subprocess.run(["git", "-C", str(REPO), "status", "--short"], capture_output=True, text=True).stdout
diff = subprocess.run(["git", "-C", str(REPO), "diff", "HEAD"], capture_output=True, text=True, encoding="utf-8",
                      errors="replace").stdout
info = {"source": str(src), "source_mtime": time.ctime(src.stat().st_mtime), "head": head,
        "dirty_files": status.splitlines(), "diff_sha256": hashlib.sha256(diff.encode()).hexdigest()[:16],
        "staged": {f.name: sha(f) for f in ARM.iterdir()}, "netbuild_done": text.strip()[:2000]}
(ARM / "commit.txt").write_text(head + "\n")
(ARM / "diff.patch").write_text(diff, encoding="utf-8")
snap = ARM / "src"
for rel in status.splitlines():
    p = rel[3:].strip()
    if rel.startswith("??") and p.startswith("src/") and (REPO / p).is_file():
        (snap / p).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / p, snap / p)
(ARM / "build-info.json").write_text(json.dumps(info, indent=1))
print(json.dumps(info, indent=1))
