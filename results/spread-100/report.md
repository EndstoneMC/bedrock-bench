# Results

Config `spread-100`, 100 bots.

## Environment

|  |  |
|---|---|
| CPU | Intel(R) Core(TM) i7-14700F |
| Server CPUs | 0-15 (16 logical) |
| Bot CPUs | 16-27 (12 logical) |
| RAM | 63.7 GiB |
| OS | Windows 11 (10.0.26200) |
| Python | 3.13.5 |
| Harness | bedrock-bench 0.1.0.dev0 (`158b54c`) |
| Harness | bedrock-bench 0.1.0.dev0 (`6d96b6b`) |
| Harness | bedrock-bench 0.1.0.dev0 (`f55fbd6`) |
| Harness | bedrock-bench 0.1.0.dev0 (`fba0cc0`) |
| Bots | loadbot, protocol 2193, gophertunnel v1.62.0 |
| Servers | `altay@5.44.7`, `bds@1.26.52.3`, `endstone@0.11.14` (bds 1.26.52.3, python 3.13), `levilamina@26.51.6` |

## Results

Median [min, max] over the ok runs.

| Server | Runs ok | TPS | Tick-thread CPU per tick (ms) | Server CPU (cores) | Memory (MiB) | Lowest TPS while views load | View load time, median player (s) | View load time, p90 player (s) | View load time, slowest player (s) | Chunks per second while views load | Chunks in view per player |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `altay@5.44.7` | 1/1 | 16.35 | 60.8 | 1.21 | 6160 | 3.95 | 33.6 | 85.6 | 97.0 | 111 | 376 |
| `bds@1.26.52.3` | 1/1 | 11.09 | 89.8 | 2.35 | 7282 | 8.86 | 530 | 679 | 876 | 47.1 | 372 |
| `endstone@0.11.14` | 1/1 | 19.92 | 50.0 | 2.43 | 8220 | 13.59 | 431 | 536 | 600 | 58.6 | 372 |
| `levilamina@26.51.6` | 1/1 | 11.29 | 88.1 | 2.40 | 8232 | 9.30 | 511 | 684 | 795 | 47.1 | 373 |
