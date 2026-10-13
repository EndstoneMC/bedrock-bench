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
| Harness | bedrock-bench 0.1.0.dev0 (`436a59d`) |
| Harness | bedrock-bench 0.1.0.dev0 (`4f763f5`) |
| Harness | bedrock-bench 0.1.0.dev0 (`553358f`) |
| Harness | bedrock-bench 0.1.0.dev0 (`6104d49`) |
| Harness | bedrock-bench 0.1.0.dev0 (`6d96b6b`) |
| Harness | bedrock-bench 0.1.0.dev0 (`7572797`) |
| Harness | bedrock-bench 0.1.0.dev0 (`7a46b55`) |
| Harness | bedrock-bench 0.1.0.dev0 (`88f8d0a`) |
| Harness | bedrock-bench 0.1.0.dev0 (`8cd8b64`) |
| Harness | bedrock-bench 0.1.0.dev0 (`aaf8cd8`) |
| Harness | bedrock-bench 0.1.0.dev0 (`bb87871`) |
| Harness | bedrock-bench 0.1.0.dev0 (`ce513e5`) |
| Harness | bedrock-bench 0.1.0.dev0 (`d0c0996`) |
| Harness | bedrock-bench 0.1.0.dev0 (`d600432`) |
| Harness | bedrock-bench 0.1.0.dev0 (`e57b3aa`) |
| Harness | bedrock-bench 0.1.0.dev0 (`f55fbd6`) |
| Harness | bedrock-bench 0.1.0.dev0 (`f98382d`) |
| Harness | bedrock-bench 0.1.0.dev0 (`fa1238f`) |
| Harness | bedrock-bench 0.1.0.dev0 (`fa3334b`) |
| Harness | bedrock-bench 0.1.0.dev0 (`fba0cc0`) |
| Harness | bedrock-bench 0.1.0.dev0 (`ff06c4b`) |
| Harness | bedrock-bench 0.1.0.dev0 (`ff12261`) |
| Bots | loadbot, protocol 2193, gophertunnel v1.62.0 |
| Servers | `altay@5.44.7`, `bds@1.26.52.3`, `dragonfly@1793369`, `endstone@0.11.14` (bds 1.26.52.3, python 3.13), `levilamina@26.51.6`, `nukkit@b3caad6`, `nukkitmot@9cb0aba`, `powernukkitx@nightly.86f70bc` |

## Results

Median [min, max] over the ok runs.

| Server | Runs ok | TPS | Tick-thread CPU per tick (ms) | Server CPU (cores) | Memory (MiB) | Lowest TPS while views load | View load time, median player (s) | View load time, p90 player (s) | View load time, slowest player (s) | Chunks per second while views load | Chunks in view per player |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `altay@5.44.7` | 3/3 | 16.38 [16.35, 16.44] | 60.6 [60.3, 60.8] | 1.23 [1.21, 1.27] | 6151 [6148, 6160] | 3.99 [3.95, 3.99] | 33.8 [33.6, 34.7] | 87.2 [85.6, 89.1] | 124 [97.0, 129] | 109 [109, 111] | 376 [376, 376] |
| `bds@1.26.52.3` | 2/2 | 11.12 [11.09, 11.15] | 89.6 [89.5, 89.8] | 2.34 [2.34, 2.35] | 7282 [7282, 7282] | 9.06 [8.86, 9.26] | 527 [525, 530] | 680 [679, 681] | 802 [728, 876] | 46.9 [46.7, 47.1] | 372 [371, 372] |
| `dragonfly@1793369` | 2/2 | 20.00 [20.00, 20.00] | n/a | 0.49 [0.49, 0.49] | 1926 [1923, 1928] | 19.85 [19.84, 19.86] | 8.60 [8.57, 8.62] | 15.4 [14.7, 16.1] | 18.7 [18.6, 18.8] | 112 [111, 113] | 371 [371, 371] |
| `endstone@0.11.14` | 2/2 | 19.80 [19.68, 19.92] | 50.3 [50.0, 50.6] | 2.44 [2.43, 2.45] | 8220 [8220, 8220] | 14.17 [13.59, 14.75] | 430 [428, 431] | 534 [532, 536] | 648 [600, 697] | 58.8 [58.6, 59.1] | 372 [372, 373] |
| `levilamina@26.51.6` | 3/3 | 11.22 [11.13, 11.29] | 88.7 [88.1, 89.6] | 2.40 [2.32, 2.43] | 8232 [8202, 8254] | 9.08 [8.90, 9.30] | 531 [511, 533] | 684 [667, 692] | 759 [689, 795] | 47.1 [46.2, 47.2] | 373 [372, 373] |
| `nukkit@b3caad6` | 0/6 | - | - | - | - | - | - | - | - | - | - |
| `nukkitmot@9cb0aba` | 0/6 | - | - | - | - | - | - | - | - | - | - |
| `powernukkitx@nightly.86f70bc` | 0/2 | - | - | - | - | - | - | - | - | - | - |

## Notes

- `dragonfly@1793369`: Tick-thread CPU per tick (ms): not applicable (the server has no single tick thread)
- `nukkit@b3caad6` run `20261013T094045Z-nukkit@b3caad6-r1-bccb`: invalid: spread: up to 29 bots in one cell; server_alive: exit code 1; joined_all: joined 72/100
- `nukkit@b3caad6` run `20261013T095506Z-nukkit@b3caad6-r1-9a58`: invalid: spread: up to 51 bots in one cell; server_alive: exit code 1; joined_all: joined 50/100
- `nukkit@b3caad6` run `20261013T113041Z-nukkit@b3caad6-r2-0dc8`: invalid: spread: up to 20 bots in one cell; server_alive: exit code 1; joined_all: joined 81/100
- `nukkit@b3caad6` run `20261013T114546Z-nukkit@b3caad6-r2-f232`: invalid: spread: up to 49 bots in one cell; server_alive: exit code 1; joined_all: joined 52/100
- `nukkit@b3caad6` run `20261013T132321Z-nukkit@b3caad6-r3-ac13`: invalid: spread: up to 43 bots in one cell; server_alive: exit code 1; joined_all: joined 58/100
- `nukkit@b3caad6` run `20261013T133741Z-nukkit@b3caad6-r3-87b7`: invalid: spread: up to 38 bots in one cell; server_alive: exit code 1; joined_all: joined 63/100
- `nukkit@b3caad6`: views were still incomplete when the settle phase ended
- `nukkitmot@9cb0aba` run `20261013T100926Z-nukkitmot@9cb0aba-r1-55d5`: invalid: spread: up to 24 bots in one cell; joined_all: joined 66/100
- `nukkitmot@9cb0aba` run `20261013T102312Z-nukkitmot@9cb0aba-r1-6193`: invalid: spread: up to 13 bots in one cell; joined_all: joined 62/100
- `nukkitmot@9cb0aba` run `20261013T120007Z-nukkitmot@9cb0aba-r2-68bf`: invalid: spread: up to 30 bots in one cell; joined_all: joined 71/100
- `nukkitmot@9cb0aba` run `20261013T121355Z-nukkitmot@9cb0aba-r2-5607`: server_failed: joined_all: joined 55/100
- `nukkitmot@9cb0aba` run `20261013T135202Z-nukkitmot@9cb0aba-r3-4e57`: invalid: spread: up to 30 bots in one cell; joined_all: joined 70/100
- `nukkitmot@9cb0aba` run `20261013T140535Z-nukkitmot@9cb0aba-r3-6bbe`: invalid: spread: up to 8 bots in one cell; joined_all: joined 67/100
- `nukkitmot@9cb0aba`: views were still incomplete when the settle phase ended
- `powernukkitx@nightly.86f70bc` run `20261013T103647Z-powernukkitx@nightly.86f70bc-r1-2233`: server_failed: server_alive: the server exited before teardown; joined_all: joined 46/100
- `powernukkitx@nightly.86f70bc` run `20261013T122711Z-powernukkitx@nightly.86f70bc-r2-bd4e`: server_failed: server_alive: the server exited before teardown; joined_all: joined 50/100
