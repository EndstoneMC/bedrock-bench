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
| Harness | bedrock-bench 0.1.0.dev0 (`02270ad`) |
| Harness | bedrock-bench 0.1.0.dev0 (`158b54c`) |
| Harness | bedrock-bench 0.1.0.dev0 (`268fbf9`) |
| Harness | bedrock-bench 0.1.0.dev0 (`2748fde`) |
| Harness | bedrock-bench 0.1.0.dev0 (`3796d4b`) |
| Harness | bedrock-bench 0.1.0.dev0 (`475ffa9`) |
| Harness | bedrock-bench 0.1.0.dev0 (`553358f`) |
| Harness | bedrock-bench 0.1.0.dev0 (`5dc7cd1`) |
| Harness | bedrock-bench 0.1.0.dev0 (`6d96b6b`) |
| Harness | bedrock-bench 0.1.0.dev0 (`6f6fa41`) |
| Harness | bedrock-bench 0.1.0.dev0 (`7a46b55`) |
| Harness | bedrock-bench 0.1.0.dev0 (`8cd8b64`) |
| Harness | bedrock-bench 0.1.0.dev0 (`96fbe05`) |
| Harness | bedrock-bench 0.1.0.dev0 (`aaf8cd8`) |
| Harness | bedrock-bench 0.1.0.dev0 (`bb87871`) |
| Harness | bedrock-bench 0.1.0.dev0 (`d0c0996`) |
| Harness | bedrock-bench 0.1.0.dev0 (`d600432`) |
| Harness | bedrock-bench 0.1.0.dev0 (`e57b3aa`) |
| Harness | bedrock-bench 0.1.0.dev0 (`ea7a3b7`) |
| Harness | bedrock-bench 0.1.0.dev0 (`f1fa1c5`) |
| Harness | bedrock-bench 0.1.0.dev0 (`f55fbd6`) |
| Harness | bedrock-bench 0.1.0.dev0 (`f98382d`) |
| Harness | bedrock-bench 0.1.0.dev0 (`fa3334b`) |
| Harness | bedrock-bench 0.1.0.dev0 (`fba0cc0`) |
| Harness | bedrock-bench 0.1.0.dev0 (`ff12261`) |
| Bots | loadbot, protocol 2193, gophertunnel v1.62.0 |
| Servers | `altay@5.44.7`, `bds@1.26.52.3`, `chorus@5c5ffd3`, `dragonfly@1793369`, `endstone@0.11.14` (bds 1.26.52.3, python 3.13), `levilamina@26.51.6`, `nukkit@b3caad6`, `nukkitmot@a27d96c`, `powernukkitx@nightly.b9b8fd8` |

## Results

Median [min, max] over the ok runs.

| Server | Runs ok | TPS | Tick-thread CPU per tick (ms) | Server CPU (cores) | Memory (MiB) | Lowest TPS while views load | View load time, median player (s) | View load time, p90 player (s) | View load time, slowest player (s) | Chunks per second while views load | Chunks in view per player |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `altay@5.44.7` | 3/3 | 16.38 [16.35, 16.44] | 60.6 [60.3, 60.8] | 1.23 [1.21, 1.27] | 6151 [6148, 6160] | 3.99 [3.95, 3.99] | 33.8 [33.6, 34.7] | 87.2 [85.6, 89.1] | 124 [97.0, 129] | 109 [109, 111] | 376 [376, 376] |
| `bds@1.26.52.3` | 3/3 | 11.15 [11.09, 11.22] | 89.5 [88.5, 89.8] | 2.34 [2.20, 2.35] | 7282 [7251, 7282] | 9.05 [8.86, 9.26] | 530 [525, 531] | 679 [679, 681] | 803 [728, 876] | 47.1 [46.7, 47.3] | 371 [371, 372] |
| `chorus@5c5ffd3` | 0/2 | - | - | - | - | - | - | - | - | - | - |
| `dragonfly@1793369` | 3/3 | 20.00 [20.00, 20.00] | n/a | 0.49 [0.49, 0.53] | 1928 [1923, 1929] | 19.85 [19.84, 19.86] | 8.60 [8.57, 8.62] | 15.5 [14.7, 16.1] | 18.8 [18.6, 20.4] | 113 [111, 113] | 371 [371, 371] |
| `endstone@0.11.14` | 3/3 | 19.81 [19.68, 19.92] | 50.3 [50.0, 50.6] | 2.43 [2.34, 2.45] | 8220 [8220, 8222] | 14.47 [13.59, 14.75] | 428 [427, 431] | 532 [531, 536] | 600 [550, 697] | 58.6 [58.5, 59.1] | 372 [370, 373] |
| `levilamina@26.51.6` | 3/3 | 11.22 [11.13, 11.29] | 88.7 [88.1, 89.6] | 2.40 [2.32, 2.43] | 8232 [8202, 8254] | 9.08 [8.90, 9.30] | 531 [511, 533] | 684 [667, 692] | 759 [689, 795] | 47.1 [46.2, 47.2] | 373 [372, 373] |
| `nukkit@b3caad6` | 0/6 | - | - | - | - | - | - | - | - | - | - |
| `nukkitmot@a27d96c` | 1/2 | 19.98 | 12.7 | 2.18 | 8723 | 19.75 | 308 | 525 | 604 | 55.5 | 371 |
| `powernukkitx@nightly.b9b8fd8` | 0/1 | - | - | - | - | - | - | - | - | - | - |

## Notes

- `chorus@5c5ffd3` run `20261013T192017Z-chorus@5c5ffd3-r1-8675`: invalid: spread: 4 cells occupied by 35 joined bots; joined_all: joined 35/100
- `chorus@5c5ffd3` run `20261018T223833Z-chorus@5c5ffd3-r1-a121`: invalid: spread: 1 cells occupied by 21 joined bots; joined_all: joined 21/100
- `chorus@5c5ffd3`: the server dropped bots during steady
- `dragonfly@1793369`: Tick-thread CPU per tick (ms): not applicable (the server has no single tick thread)
- `nukkit@b3caad6` run `20261013T094045Z-nukkit@b3caad6-r1-bccb`: invalid: spread: up to 29 bots in one cell; joined_all: joined 72/100
- `nukkit@b3caad6` run `20261013T095506Z-nukkit@b3caad6-r1-9a58`: invalid: spread: up to 51 bots in one cell; joined_all: joined 50/100
- `nukkit@b3caad6` run `20261013T113041Z-nukkit@b3caad6-r2-0dc8`: invalid: spread: up to 20 bots in one cell; joined_all: joined 81/100
- `nukkit@b3caad6` run `20261013T114546Z-nukkit@b3caad6-r2-f232`: invalid: spread: up to 49 bots in one cell; joined_all: joined 52/100
- `nukkit@b3caad6` run `20261013T132321Z-nukkit@b3caad6-r3-ac13`: invalid: spread: up to 43 bots in one cell; joined_all: joined 58/100
- `nukkit@b3caad6` run `20261013T133741Z-nukkit@b3caad6-r3-87b7`: invalid: spread: up to 38 bots in one cell; joined_all: joined 63/100
- `nukkit@b3caad6`: the server crashed or exited with an error when stopped, after the measured windows
- `nukkit@b3caad6`: views were still incomplete when the settle phase ended
- `nukkitmot@a27d96c` run `20261018T220053Z-nukkitmot@a27d96c-r1-3113`: invalid: foreign_load: foreign load 10 s peak 5.7639 cores in load (limit 5)
- `nukkitmot@a27d96c`: the server crashed or exited with an error when stopped, after the measured windows
- `powernukkitx@nightly.b9b8fd8` run `20261017T232202Z-powernukkitx@nightly.b9b8fd8-r1-7f4e`: server_failed: server_alive: the server exited before teardown; joined_all: joined 89/100
- `powernukkitx@nightly.b9b8fd8`: the server dropped bots during steady
