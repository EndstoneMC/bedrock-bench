"""Per-bot packet rates the bots received in the last PKT dump before chaos started (or before the end of the run).

  python -I pktmix.py <bots-a.log> [...]
"""

import json
import re
import sys
from pathlib import Path

PKT = re.compile(r"PKT id=(\d+) name=\*packet\.(\w+) rate/s=([\d.]+) per-bot/s=([\d.]+)")


def last_block(path):
    blocks, cur, last_t = [], {}, None
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if "CTL jitter on" in line:
            break
        m = PKT.search(line)
        if m:
            t = line.split()[0][:8]
            if t != last_t and cur:
                blocks.append(cur)
                cur = {}
            last_t = t
            cur[m.group(2)] = float(m.group(4))
        elif " DONE " in line:
            break
    if cur:
        blocks.append(cur)
    return blocks[-1] if blocks else {}


if __name__ == "__main__":
    out = {}
    for p in sys.argv[1:]:
        b = last_block(p)
        out[p] = dict(sorted(b.items(), key=lambda kv: -kv[1]))
        print(p)
        for k, v in list(out[p].items())[:14]:
            print(f"  {k:32s} {v:8.2f}/bot/s")
    if len(sys.argv) > 1:
        Path(sys.argv[1]).with_name("pktmix.json").write_text(json.dumps(out, indent=1))
