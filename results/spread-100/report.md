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
| Bots | loadbot, protocol 2193, gophertunnel v1.62.0 |
| Servers | `bds@1.26.52.3` |

## Results

Median [min, max] over the ok runs.

| Server | Runs ok | TPS | Tick-thread CPU per tick (ms) | Server CPU (cores) | Memory (MiB) | Lowest TPS while views load | View load time, median player (s) | View load time, p90 player (s) | View load time, slowest player (s) | Chunks per second while views load | Chunks in view per player |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bds@1.26.52.3` | 1/1 | 11.09 | 89.8 | 2.35 | 7282 | 8.86 | 530 | 679 | 876 | 47.1 | 372 |
