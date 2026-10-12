# Provenance of the Endstone 0.11.14 benchmark figures

This document maps every figure in Endstone 0.11.14's
[BENCHMARK.md](https://github.com/EndstoneMC/endstone/blob/v0.11.14/BENCHMARK.md) (added in Endstone commit
`622de76b2`, released with tag `v0.11.14`) to the run, the server build and the bot binary that produced it, and to the
archived summary that shows the number. The runs used the scripts in `legacy/`, before bedrock-bench existed.

The archived summaries are in [`results/`](results/), with checksums in [`SHA256SUMS`](SHA256SUMS). They are the
harness's own console summaries and result files, copied from `legacy/results/` with line endings normalised to LF;
`results/windows/chaos-after-win.out` was also converted from UTF-16 to UTF-8 (original sha256
`481ed1226b5c3c74c50128e4bb78989d1dc7601e2401bd024447021be5c27779`). The raw run directories (probe series, thread
series, bot logs, server logs) were not kept, so figures computed from them by post-processing scripts can be traced to
their script and run but not recomputed. Every figure below is a single run.

Development runs from the Endstone 0.11.14 work that back no published figure are not archived here.

## Common conditions

| | Windows | Linux |
|---|---|---|
| Machine | Intel Core i7-14700F (8 performance cores with SMT, 12 efficiency cores), Windows 11 | same machine, Docker Desktop on WSL2 |
| Placement | server on logical CPUs 0-15 (performance cores), bots on 16-27 (efficiency cores), server at above-normal priority | `--cpuset-cpus` 0-15 for the server and 16-27 for the bots; these are WSL2 virtual CPUs, not physical cores |
| Server | BDS 1.26.52.3, fresh world, seed 20261010, view distance 10, tick distance 4, client-side generation on, default `max-threads` | same |
| Load | 100 bots, joins 300 ms apart, 11-column grid 640 blocks apart, `tp` to y = 300 with effects | same, bots in the server container's network namespace |
| Phases | 20 s idle, 12 s pause, join, settle (at least 300 s, ending at 0.1 new chunks per bot per second), about 15 s of spread check, 90 s steady | settle 300 to 900 s with the same end rate, 90 s steady |
| Harness | `legacy/windows/run_endstone.py` | `legacy/linux/bench.py` and `chaos.py`, metrics by `metrics_linux.py` |
| Bot binary (steady) | `loadbot.exe`, a build from before the chaos features; its exact source was not kept | `loadbot-linux`; its exact source was not kept |
| Bot binary (chaos) | `loadbot-chaos.exe` | `loadbot-linux-chaos`, which jittered its view radius from the moment it joined |

The closest surviving bot sources are `legacy/bots/gophertunnel-fork/cmd/loadbot/main.go` (the chaos-capable bot),
`legacy/bots/gtfork/main.go` (fake XUIDs) and `legacy/bots/gt1001/main.go` (protocol 1001). No bot binary hash was
recorded.

## Endstone 0.11.14, Windows table

### Runs and builds

| Column | Steady run | Chaos run | Build |
|---|---|---|---|
| Vanilla BDS 1.26.52 | `vanilla-100`, 2026-10-11 00:19 | `chaos-vanilla-win`, 2026-10-11 12:54 | `bedrock_server.exe` 1.26.52.3 run directly |
| Endstone 0.11.13 | `begin-01113`, 2026-10-10 22:41 | `chaos-01113-win`, 2026-10-11 12:12 | PyPI wheel 0.11.13, unmodified (arm `dll-pypi`) |
| Endstone 0.11.14 | `final-100`, 2026-10-12 01:21 | `chaos-after-win`, 2026-10-10 23:15 | steady: a pre-release `endstone_runtime.dll` (sha256 prefix `85262df56bbd64e4`, arm `dll-final`) copied into the 0.11.13 PyPI install; chaos: an earlier development DLL (prefix `ed43d5e170ccb982`, arm `dll-after`) from before the packet compression and terrain generation changes |

The commit behind either 0.11.14 DLL is not recorded. Because the runtime library was swapped into a 0.11.13 install,
the server's version banner reads 0.11.13 in both 0.11.14 runs.

### Figures

| Row | Vanilla | 0.11.13 | 0.11.14 | Source and derivation |
|---|--:|--:|--:|---|
| Steady TPS | 9.40 | 11.28 | 20.0 | `windows/<run>.out`, line `tps steady`, `median`: median of the 5 s probe samples in the 90 s steady window |
| Main thread per tick | 105 ms | 89 ms | 43 ms | `windows/<run>.out`, line `window steady_unprof`: `MC_SERVER` ms/s divided by that line's window TPS (971 / 9.237, 996 / 11.232, 857 / 20.001). Note the divisor differs from the Steady TPS row |
| View load time, median | 192 s | 189 s | 81 s | `windows/<run>.out`, `t90_after_tp_s` `median` (191.47, 189.03, 80.54): per bot, time from teleport to 90% of its final unique chunks, read from bot summaries printed every 60 s. Vanilla's 192 comes from the reference check's rounded 191.5 (`levilamina/_ref_check.md`) |
| TPS under chaos, normal tick speed | 11.8 | 8.9 | 19.4 | chaos runs above, computed by `legacy/windows/chains/wf_chaos.py` from the raw run directory: samples labelled by the random tick speed applied (per the server log) at their end, median of those at 0, 1 or 3. Whether the 10 s monitor or the 5 s probe series was published is not recorded. These runs settled for 120 to 300 s, then ran 10 minutes of chaos instead of the steady window. `windows/chaos-*.out` holds the 10 s monitor samples (`MON` lines) |
| CPU cores, steady | 1.07 | 1.81 | 1.04 | vanilla and 0.11.14: `levilamina/_ref_check.md`, "process cores steady", computed by `legacy/levilamina/harness/levi_metrics.py` as the sum of per-thread CPU time over threads alive at both ends of the steady window, divided by its length. 0.11.13: not in the archive; it came from `legacy/windows/chains/wf_cores.py` over the raw thread series |
| Memory (private) | 4.1 GB | 4.9 GB | 4.9 GB | vanilla and 0.11.14: `windows/<run>.out`, `window steady_unprof`, `mem` `private` (4063.0 and 4857.4 MiB), divided by 1000 and labelled GB (3.97 and 4.74 GiB). 0.11.13: not in the archive (`begin-01113.out` predates the memory column) |

Footnotes:

- ¹ (chaos on a development build): see the Endstone 0.11.14 chaos run above.
- ² (DevTools spin): `windows/begin-01113.out`, `window steady_unprof`, thread `DevTools` at 701 ms/s.
- ³ is in the Linux table.

### Join dip paragraph

"The lowest 15-second average was 17.5 on Windows and 8.0 on Linux."

- Windows 17.5: `levilamina/_ref_check.md`, "loading TPS (join+settle) ... lowest 15 s" for endstone-0.11.14, 17.46,
  computed by `levi_metrics.py` from the `final-100` probe series.
- Linux 8.0: `linux/wake0-100.json`, `load_tps_min15` = 7.95.

## Endstone 0.11.14, Linux table

| Column | Steady run | Chaos run | Build |
|---|---|---|---|
| Vanilla BDS 1.26.52 | `vanilla-100` | `chaos-vanilla` | BDS 1.26.52.3 run directly inside the Endstone 0.11.14.dev4 image |
| Endstone 0.11.13 | `pypi01113-100` | `chaos-pypi01113` | PyPI wheel `endstone-0.11.13-cp314-cp314-manylinux_2_31_x86_64` |
| Endstone 0.11.14 | `wake0-100` | `chaos-after-r2` | steady: development wheel 0.11.14.dev10 (recorded as Endstone commit `97d48a04d`); chaos: 0.11.14.dev4 |

| Row | Vanilla | 0.11.13 | 0.11.14 | Source (`linux/<run>.json`) |
|---|--:|--:|--:|---|
| Steady TPS | 9.46 | 9.45 | 20.0 | `steady_tps_window`: TPS over the whole 90 s steady window (the Windows row uses the median of 5 s samples) |
| Main thread per tick | 105.7 ms | 105.9 ms | 46.5 ms | `mc_ms_tick`: CPU of the lowest-tid `MC_SERVER` thread per second divided by window TPS |
| View load time, median | 194 s | 192 s | 130 s | `view_load_s.median` (194.20, 192.23, 130.49) |
| TPS under chaos, normal tick speed | 14.8 | 10.2 | 19.3 | chaos run's `mon10_normal` (14.84, 10.15, 19.30): median of 10 s monitor samples labelled by the applied random tick speed at their end |
| CPU cores, steady | 1.09 | 1.11 | 1.09 | `steady_proc_cores` |
| Memory (RSS) | 3.96 GB | 4.06 GB | 4.54 GB | `rss_steady_mb` (3955, 4059, 4539 MiB) divided by 1000 and labelled GB |

Footnote ³ (mimalloc): Endstone 0.11.14 preloads mimalloc on Linux; Endstone 0.11.13 does not.

`linux/chaos-after.json` is an earlier 0.11.14.dev4 chaos run (17.16 at normal tick speed) that overlapped a Windows
chaos run on the same machine. It was discarded and rerun as `chaos-after-r2`; it is archived for completeness.

## Other Bedrock servers table

All rows except Endstone come from `custom-servers/comparison.md`, produced by `legacy/custom-servers/scripts/cbench.py`
and `csummary.py` (Windows, same machine and CPU sets, settle capped at 600 s, a 5-minute chaos after steady). The raw
run directories were not kept.

| Row | Run tag | Server build | Bot binary | Steady TPS | Lowest TPS while views load | CPU cores, steady | Memory |
|---|---|---|---|--:|--:|--:|--:|
| Endstone 0.11.14 | `final-100` (Windows table above) | as above | `loadbot.exe` | 20.0 | 16.5 | 1.04 | 4.9 GB |
| Dragonfly | `dragonfly-100` | commit `1793369`, flat world | `loadbot-chaos.exe` | 20.0 (19.99) | 14.6 (14.57) | 0.56 | 1.9 GB (1921 MiB private) |
| Nukkit | `nukkit-100-pregen` | commit `b3caad6`, pre-generated world | `loadbot-2193x.exe` (fake XUIDs) | 20.0 (20.00) | 0.7 (0.69) | 1.32 | 5.0 GB (5004 MiB private) |
| PocketMine-MP | `pmmp-100-nolimit` | 5.44.3, memory limits set to 0 | `loadbot-1001.exe` (protocol 1001) | 4.6 (4.62) | 1.5 (1.54) | 1.07 | 6.2 GB (6193 MiB private) |
| PowerNukkitX | `pnx-100` | 3.0.5, fresh world, JVM default heap | `loadbot-2193x.exe` | 1.3 (1.34) | 0.7 (0.69) | 3.67 | 16.1 GB (16136 MiB private) |

- "Lowest TPS while views load" is the minimum 5 s probe sample over join and settle ("loading TPS ... min" in
  `comparison.md`). For Endstone it is `final-100`'s `tps settle` minimum, 16.53, in `windows/final-100.out`.
- "4.3 TPS on a pre-generated world" for PowerNukkitX is run `pnx-100-pregen2`, steady median 4.25.
- "about 450 chunks to each player, where Endstone sends about 100": unique chunks per bot, medians 453 to 480 for the
  servers that sent the full disc, 99 to 103 for Endstone (`comparison.md`, `levilamina/_ref_check.md`). PowerNukkitX's
  fresh-world views never filled (median 16).
- "None of these servers implements the locator bar or server-side player movement checks": zero movement corrections
  and zero locator-bar packets in the bots' packet counts, plus each server's source. The packet dumps were not kept.
- Nukkit on a fresh world ("shut down at its first autosave"): run `nukkit-100`, not archived. 37 of 100 bots joined
  before its watchdog stopped the server.
- The Endstone column of `comparison.md` itself (`custom-servers/endstone-ref.json`) was taken from the development run
  `chaos-after-win`, not from `final-100`; BENCHMARK.md used `final-100` for the Endstone row.

Differences from the Endstone runs that the table did not state: PocketMine-MP and Nukkit ran at difficulty normal
while BDS ran at easy; PocketMine-MP's TPS came from world time (`time query`) rather than a per-tick counter; bots ran
with `-csg` on every server, which servers without client-side generation ignore.

## Methodology, equivalence and correctness sections

- "Main thread per tick", "View load time", "CPU cores and memory" definitions: see the derivations above. Memory "GB"
  values are MiB divided by 1000.
- "Equivalence" (0 differences in 745 million checks on Windows and 833 million on Linux) and "Correctness" (208 of 208
  in-server tests) come from Endstone's own instrumented builds and test plugin, run with the same harness on separate
  occasions. They are Endstone's evidence about Endstone and are not part of this archive.

## Archive contents

| File | Run | Used for |
|---|---|---|
| `results/windows/vanilla-100.out` | vanilla steady, Windows | Windows table, vanilla |
| `results/windows/begin-01113.out` | 0.11.13 steady, Windows | Windows table, 0.11.13; footnote ² |
| `results/windows/final-100.out` | 0.11.14 steady, Windows | Windows table, 0.11.14; other-servers Endstone row |
| `results/windows/chaos-vanilla-win.out` | vanilla chaos, Windows | chaos monitor samples |
| `results/windows/chaos-01113-win.out` | 0.11.13 chaos, Windows | chaos monitor samples |
| `results/windows/chaos-after-win.out` | 0.11.14 development chaos, Windows | chaos monitor samples |
| `results/linux/vanilla-100.json` | vanilla steady, Linux | Linux table, vanilla |
| `results/linux/pypi01113-100.json` | 0.11.13 steady, Linux | Linux table, 0.11.13 |
| `results/linux/wake0-100.json` | 0.11.14.dev10 steady, Linux | Linux table, 0.11.14; join dip |
| `results/linux/chaos-vanilla.json` | vanilla chaos, Linux | Linux chaos, vanilla |
| `results/linux/chaos-pypi01113.json` | 0.11.13 chaos, Linux | Linux chaos, 0.11.13 |
| `results/linux/chaos-after-r2.json` | 0.11.14.dev4 chaos, Linux | Linux chaos, 0.11.14 |
| `results/linux/chaos-after.json` | 0.11.14.dev4 chaos, contaminated | not used |
| `results/custom-servers/comparison.md` | other servers | other-servers table |
| `results/custom-servers/endstone-ref.json` | Endstone development run | Endstone column of `comparison.md` |
| `results/levilamina/_ref_check.md` | vanilla and 0.11.14 reference check, Windows | CPU cores, view load rounding, join dip |
| `results/levilamina/_interim.md` | LeviLamina comparison, interim | not published |
| `results/levilamina/chaos-levi-opt.wf_chaos.json` | LeviLamina chaos | not published |

The LeviLamina files are interim results that BENCHMARK.md does not use; one of their runs is marked contaminated in
the file itself.
