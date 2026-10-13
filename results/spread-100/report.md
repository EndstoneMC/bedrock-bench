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
| Harness | bedrock-bench 0.1.0.dev0 (`2748fde`) |
| Harness | bedrock-bench 0.1.0.dev0 (`6104d49`) |
| Harness | bedrock-bench 0.1.0.dev0 (`6d96b6b`) |
| Harness | bedrock-bench 0.1.0.dev0 (`88f8d0a`) |
| Harness | bedrock-bench 0.1.0.dev0 (`ce513e5`) |
| Harness | bedrock-bench 0.1.0.dev0 (`d0c0996`) |
| Harness | bedrock-bench 0.1.0.dev0 (`f55fbd6`) |
| Harness | bedrock-bench 0.1.0.dev0 (`fba0cc0`) |
| Bots | loadbot, protocol 2193, gophertunnel v1.62.0 |
| Servers | `altay@5.44.7`, `bds@1.26.52.3`, `dragonfly@1793369`, `endstone@0.11.14` (bds 1.26.52.3, python 3.13), `levilamina@26.51.6`, `nukkit@b3caad6`, `nukkitmot@9cb0aba`, `powernukkitx@nightly.86f70bc` |

## Results

Median [min, max] over the ok runs.

| Server | Runs ok | TPS | Tick-thread CPU per tick (ms) | Server CPU (cores) | Memory (MiB) | Lowest TPS while views load | View load time, median player (s) | View load time, p90 player (s) | View load time, slowest player (s) | Chunks per second while views load | Chunks in view per player |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `altay@5.44.7` | 1/1 | 16.35 | 60.8 | 1.21 | 6160 | 3.95 | 33.6 | 85.6 | 97.0 | 111 | 376 |
| `bds@1.26.52.3` | 1/1 | 11.09 | 89.8 | 2.35 | 7282 | 8.86 | 530 | 679 | 876 | 47.1 | 372 |
| `dragonfly@1793369` | 1/1 | 20.00 | n/a | 0.49 | 1923 | 19.86 | 8.62 | 16.1 | 18.6 | 111 | 371 |
| `endstone@0.11.14` | 1/1 | 19.92 | 50.0 | 2.43 | 8220 | 13.59 | 431 | 536 | 600 | 58.6 | 372 |
| `levilamina@26.51.6` | 1/1 | 11.29 | 88.1 | 2.40 | 8232 | 9.30 | 511 | 684 | 795 | 47.1 | 373 |
| `nukkit@b3caad6` | 0/2 | - | - | - | - | - | - | - | - | - | - |
| `nukkitmot@9cb0aba` | 0/2 | - | - | - | - | - | - | - | - | - | - |
| `powernukkitx@nightly.86f70bc` | 0/1 | - | - | - | - | - | - | - | - | - | - |

## Notes

- `dragonfly@1793369`: Tick-thread CPU per tick (ms): not applicable (the server has no single tick thread)
- `nukkit@b3caad6` run `20261013T094045Z-nukkit@b3caad6-r1-bccb`: invalid: spread: up to 29 bots in one cell; server_alive: exit code 1; joined_all: joined 72/100
- `nukkit@b3caad6` run `20261013T095506Z-nukkit@b3caad6-r1-9a58`: invalid: spread: up to 51 bots in one cell; server_alive: exit code 1; joined_all: joined 50/100
- `nukkit@b3caad6`: views were still incomplete when the settle phase ended
- `nukkitmot@9cb0aba` run `20261013T100926Z-nukkitmot@9cb0aba-r1-55d5`: invalid: spread: up to 24 bots in one cell; joined_all: joined 66/100
- `nukkitmot@9cb0aba` run `20261013T102312Z-nukkitmot@9cb0aba-r1-6193`: invalid: spread: up to 13 bots in one cell; joined_all: joined 62/100
- `nukkitmot@9cb0aba`: views were still incomplete when the settle phase ended
- `powernukkitx@nightly.86f70bc` run `20261013T103647Z-powernukkitx@nightly.86f70bc-r1-2233`: server_failed: server_alive: the server exited before teardown; joined_all: joined 46/100
