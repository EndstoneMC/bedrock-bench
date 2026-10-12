# Reproducing Endstone's 0.11.14 figures

Endstone's [BENCHMARK.md](https://github.com/EndstoneMC/endstone/blob/main/BENCHMARK.md) for 0.11.14 compares
`bds@1.26.52.3`, `endstone@0.11.13` and `endstone@0.11.14` under 100 spread players with client-side generation on.
Those figures came from the scripts now in `legacy/`. This page explains how to check them with bedrock-bench, what
agreement to expect, and what BENCHMARK.md got wrong or left out. Where each published figure came from is in
[PROVENANCE.md](../published/legacy-0.11.14/PROVENANCE.md).

bedrock-bench's headline runs Endstone with client-side generation off, like every other server
([ADR 0005](adr/0005-csg-off-headline.md)), so its numbers will not match BENCHMARK.md. The published figures are
checked with a separate matrix, `endstone-0.11.14-csg`.

## What you need

- A Windows 10/11 or Linux x86_64 machine, quiet for about three hours. Bare metal for Linux.
- At least 8 logical CPUs for the server and 4 more for the bots. The reference machine is an Intel Core i7-14700F
  (8 performance cores with SMT, 12 efficiency cores), Windows 11, server on logical CPUs 0-15, bots on 16-27.
- Python 3.11+, uv, and Go 1.25+ (or a release bot binary).

Everything else is downloaded from public sources: BDS from Mojang, Endstone from PyPI, the bot built from this
repository.

## Steps

```shell
uv sync
uv run bbench doctor
uv run bbench fetch endstone-0.11.14-csg --accept-eula
uv run bbench bots build
uv run bbench smoke bds@1.26.52.3
uv run bbench run endstone-0.11.14-csg
uv run bbench report runs/endstone-0.11.14-csg --claims endstone-0.11.14-csg
```

`matrices/endstone-0.11.14-csg.toml` runs three arms, each three times in rotated order with a 60 s cooldown:

| Arm | Server | Install |
|---|---|---|
| `bds@1.26.52.3` | vanilla BDS | Mojang zip, sha256-pinned |
| `endstone@0.11.13` | Endstone on `bds@1.26.52.3` | `pip install endstone==0.11.13` with hash checking, Python 3.13 |
| `endstone@0.11.14` | Endstone on `bds@1.26.52.3` | `pip install endstone==0.11.14` with hash checking, Python 3.13 |

The scenario is `spread100-csg`, which keeps the legacy workload: 100 bots 640 blocks apart, view distance 10,
simulation distance 4, client-side generation on, seed 20261010, 5 s probe, settle between 300 and 900 s ending at 0.1
new chunks per bot per second, 90 s steady window. The matrix uses `above_normal` priority like the legacy runs. A chaos
scenario can be added to the matrix; chaos takes another 15 minutes per run.

`matrices/endstone-0.11.14.toml` runs the same arms on the `spread100` headline, which differs only in turning
client-side generation off. It has no published figure to compare against; its first publication sets the baseline.

Expect 9 runs of 10 to 20 minutes each, plus cooldowns.

## What to expect

`claims/endstone-0.11.14-csg.toml` states what should hold on any machine, and `report --claims` checks it:

| Claim | Check | Inconclusive when |
|---|---|---|
| `endstone@0.11.14` holds 20 TPS | median steady TPS of `endstone@0.11.14` at least 19.8 | |
| `endstone@0.11.14` is much faster than vanilla | median steady TPS ratio `endstone@0.11.14` / `bds@1.26.52.3` at least 1.8 | vanilla itself holds 19.5 TPS or more on your machine |
| Tick-thread CPU per tick matches the published 43 ms | within ±15% of 42.9 ms | checked on the reference machine only |

On a machine much faster than the reference, vanilla may not saturate, and the TPS ratio says nothing; compare
tick-thread CPU per tick between the arms instead. On a slower machine, `endstone@0.11.14` may itself fall below
20 TPS.

Figures that should differ from BENCHMARK.md even on the reference machine, and why:

- **View-load time is lower and finer.** The legacy figure was read from bot summaries printed every 60 s and counted
  chunks streamed for a bot's old position. The new figures use the time each in-view chunk was first seen.
- **Nudge counts change.** Nudges now act on data at most 5 s old instead of up to 60 s.
- **The bot is different.** The published steady figures used a bot binary whose source was not kept; Linux chaos used
  a variant that jittered the view radius from the moment it joined; Windows chaos used a separate chaos build. The new
  bot is one source with per-session counters and start directions spread by the golden angle.
- **Endstone builds are the released wheels.** The published Linux `endstone@0.11.14` steady figures came from the
  development wheel 0.11.14.dev10 and the chaos figure from 0.11.14.dev4; the Windows `endstone@0.11.14` figures came
  from a pre-release runtime library inside an `endstone@0.11.13` install. The new matrix installs the released
  `endstone@0.11.14` wheel.
- **`endstone@0.11.13` CPU includes its DevTools spin.** About 0.7 of a core in `endstone@0.11.13` goes to a DevTools
  thread that spins (fixed in `endstone@0.11.14`). It is footnoted as a known issue and not subtracted.
- **Single runs become medians.** Each legacy figure is one run. Run-to-run noise on tick-thread CPU is about ±15 ms per
  second.
- **The cross-server table is not reproduced as published.** Its columns came from a different script with different
  phases, bot builds and settings (below). Cross-server comparisons use `matrices/cross-server.toml` with
  `spread100`; only that matrix is comparable across server families.

## Corrections to BENCHMARK.md

These statements in BENCHMARK.md are inaccurate or incomplete. None of them changes the direction of the Endstone
comparison, but they matter for anyone reproducing the numbers.

1. **"100 headless clients join at the same time."** The legacy bot started one join every 300 ms; all 100 had joined
   30 to 35 s after the first dial. The new harness does the same and says so.
2. **"Linux runs use Docker on WSL2 with the same pinning."** `--cpuset-cpus` under WSL2 pins virtual CPUs, which
   Hyper-V does not tie to the host's performance cores. The Linux columns are not pinned to physical cores.
3. **Memory "GB" is MiB divided by 1000.** For example, `endstone@0.11.14` on Windows used 4,857 MiB of private
   memory, published as 4.9 GB; that is 4.7 GiB. Vanilla Linux's 3.96 GB is 3,955 MiB, or 3.9 GiB. The new report uses
   MiB and GiB only.
4. **"Main thread per tick" is not milliseconds per tick.** It is tick-thread CPU per second divided by TPS. It excludes
   time the tick thread is blocked, and on Windows it was divided by the TPS over the whole 90 s window while the
   "Steady TPS" row shows the median of 5 s samples (vanilla: 9.40 median, 9.24 over the window). The new report calls
   it tick-thread CPU per tick and divides CPU time by ticks executed ([ADR 0003](adr/0003-tick-cpu-is-not-mspt.md)).
5. **Two different "lowest TPS" figures.** The "Lowest TPS while views load" column is the lowest 5 s value (16.5 for
   `endstone@0.11.14` on Windows). The paragraph's 17.5 (Windows) and 8.0 (Linux) are lowest 15 s values. Both are now
   reported under their own names.
6. **Chaos TPS** is a median of short TPS samples (the chaos monitor's roughly 10 s samples on Linux; on Windows the
   archive does not record whether the 10 s or the 5 s series was published), each labelled by the tick speed in effect
   at the end of the sample according to the server's "applied" log lines. Samples that straddled a tick-speed change
   were not excluded. Windows chaos ran 10 minutes after a 120 to 300 s settle, Linux chaos 15 minutes. The new harness
   excludes straddling samples and uses one chaos length.
7. **CPU cores on Windows** came from a script that converted per-thread cycle counts with a fixed 2.1 GHz frequency and
   also offered thread-time-based figures; which of the two was published is not recorded. The new harness uses process
   CPU counters.
8. **"Other Bedrock servers" were not measured "with the same harness".** They used a separate script (`cbench.py`)
   with a settle cap of 600 s instead of 900 s and a 5-minute chaos after steady, and a different bot build per server:
   the chaos build for Dragonfly, a protocol 1001 build for PocketMine-MP, and a build with fake XUIDs for Nukkit and
   PowerNukkitX. PocketMine-MP and Nukkit ran at difficulty normal while BDS ran at easy. PocketMine-MP's TPS came from
   world time (`time query`), not a per-tick counter. Nukkit ran on a pre-generated world, PowerNukkitX with the JVM
   default heap. The Endstone row in that table came from the Endstone harness's run. The new cross-server matrix runs
   every server through one harness with one bot source and aligned settings.
9. **Equivalence and test-suite results** ("0 differences in 745 million checks", "208 of 208 tests") are evidence from
   Endstone's own instrumented builds and test plugin. They are not produced or reproducible by this harness, which
   relies on declared parity and observed counters instead.

## Reporting a reproduction

Open an issue or a pull request with:

- `runs/endstone-0.11.14-csg/summary.json`, `report.md` and the claims output;
- `bbench doctor --json`;
- every run's `run.json` and `metrics.json` (raw bundles as an attachment if you can).

A reproduction on different hardware is useful even when a claim comes out inconclusive. Say which machine it was.
