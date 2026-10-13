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
| Harness | bedrock-bench 0.1.0.dev0 (`0224d02`) |
| Harness | bedrock-bench 0.1.0.dev0 (`0afe819`) |
| Harness | bedrock-bench 0.1.0.dev0 (`341131a`) |
| Harness | bedrock-bench 0.1.0.dev0 (`54b4285`) |
| Harness | bedrock-bench 0.1.0.dev0 (`a9abdff`) |
| Bots | loadbot, protocol 2193, gophertunnel v1.62.0 |
| Servers | `altay@5.44.7`, `bds@1.26.52.3`, `endstone@0.11.14` (bds 1.26.52.3, python 3.13), `levilamina@26.51.6`, `nukkit@b3caad6` |

## Results

Median [min, max] over the ok runs.

| Server | Runs ok | TPS | Tick-thread CPU per tick (ms) | Server CPU (cores) | Memory (MiB) | Lowest TPS while views load | View load time, median player (s) | View load time, p90 player (s) | View load time, slowest player (s) | Chunks per second while views load | Chunks in view per player |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `altay@5.44.7` | 1/1 | - | 63.7 | 1.11 | 6027 | 3.95 | 30.7 | 45.6 | 73.4 | 111 | 376 |
| `bds@1.26.52.3` | 1/1 | 11.27 | 88.4 | 2.39 | 7290 | 9.03 | 525 | 696 | 706 | 46.3 | 372 |
| `endstone@0.11.14` | 1/1 | 19.76 | 50.1 | 2.39 | 8214 | 15.25 | 427 | 527 | 554 | 59.2 | 372 |
| `levilamina@26.51.6` | 1/1 | 11.09 | 89.8 | 2.21 | 8320 | 9.04 | 517 | 672 | 778 | 47.1 | 371 |
| `nukkit@b3caad6` | 0/1 | - | - | - | - | - | - | - | - | - | - |

## Notes

- `altay@5.44.7`: TPS: unavailable in 1 of 1 ok runs (probe replies cover less than 90% of the window)
- `nukkit@b3caad6` run `20261013T002941Z-nukkit@b3caad6-r1-8efb`: server_failed: server_alive: the server exited before teardown; joined_all: joined 54/100
