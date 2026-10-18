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
| Harness | bedrock-bench 0.1.0.dev0 (`268fbf9`) |
| Harness | bedrock-bench 0.1.0.dev0 (`3796d4b`) |
| Harness | bedrock-bench 0.1.0.dev0 (`475ffa9`) |
| Harness | bedrock-bench 0.1.0.dev0 (`5dc7cd1`) |
| Harness | bedrock-bench 0.1.0.dev0 (`6f6fa41`) |
| Harness | bedrock-bench 0.1.0.dev0 (`8cd8b64`) |
| Harness | bedrock-bench 0.1.0.dev0 (`ea7a3b7`) |
| Harness | bedrock-bench 0.1.0.dev0 (`f1fa1c5`) |
| Harness | bedrock-bench 0.1.0.dev0 (`f98382d`) |
| Bots | loadbot, protocol 2193, gophertunnel v1.62.0 |
| Servers | `altay@5.44.7`, `bds@1.26.52.3`, `chorus@5c5ffd3`, `dragonfly@1793369`, `endstone@0.11.14` (bds 1.26.52.3, python 3.13), `levilamina@26.51.6`, `nukkit@b3caad6`, `nukkitmot@a27d96c`, `powernukkitx@nightly.b9b8fd8` |

## Results

Median [min, max] over the ok runs.

| Server | Runs ok | TPS | Tick-thread CPU per tick (ms) | Server CPU (cores) | Memory (MiB) | Lowest TPS while views load | View load time, median player (s) | View load time, p90 player (s) | View load time, slowest player (s) | Chunks per second while views load | Chunks in view per player |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `altay@5.44.7` | 1/1 | 16.38 | 60.6 | 1.27 | 6151 | 3.99 | 34.7 | 89.1 | 129 | 109 | 376 |
| `bds@1.26.52.3` | 1/1 | 11.22 | 88.5 | 2.20 | 7251 | 9.05 | 531 | 679 | 803 | 47.3 | 371 |
| `chorus@5c5ffd3` | 0/1 | - | - | - | - | - | - | - | - | - | - |
| `dragonfly@1793369` | 1/1 | 20.00 | n/a | 0.53 | 1929 | 19.85 | 8.60 | 15.5 | 20.4 | 113 | 371 |
| `endstone@0.11.14` | 1/1 | 19.81 | 50.3 | 2.34 | 8222 | 14.47 | 427 | 531 | 550 | 58.5 | 370 |
| `levilamina@26.51.6` | 1/1 | 11.22 | 88.7 | 2.43 | 8254 | 9.08 | 531 | 667 | 689 | 47.2 | 373 |
| `nukkit@b3caad6` | 0/1 | - | - | - | - | - | - | - | - | - | - |
| `nukkitmot@a27d96c` | 1/1 | 19.98 | 12.7 | 2.18 | 8723 | 19.75 | 308 | 525 | 604 | 55.5 | 371 |
| `powernukkitx@nightly.b9b8fd8` | 0/1 | - | - | - | - | - | - | - | - | - | - |

## Notes

- `chorus@5c5ffd3` run `20261018T223833Z-chorus@5c5ffd3-r1-a121`: invalid: spread: 1 cells occupied by 21 joined bots; joined_all: joined 21/100
- `chorus@5c5ffd3`: the server dropped bots during steady
- `dragonfly@1793369`: Tick-thread CPU per tick (ms): not applicable (the server has no single tick thread)
- `nukkit@b3caad6` run `20261013T133741Z-nukkit@b3caad6-r3-87b7`: invalid: spread: up to 38 bots in one cell; joined_all: joined 63/100
- `nukkit@b3caad6`: the server crashed or exited with an error when stopped, after the measured windows
- `nukkit@b3caad6`: views were still incomplete when the settle phase ended
- `nukkitmot@a27d96c`: the server crashed or exited with an error when stopped, after the measured windows
- `powernukkitx@nightly.b9b8fd8` run `20261017T232202Z-powernukkitx@nightly.b9b8fd8-r1-7f4e`: server_failed: server_alive: the server exited before teardown; joined_all: joined 89/100
- `powernukkitx@nightly.b9b8fd8`: the server dropped bots during steady
