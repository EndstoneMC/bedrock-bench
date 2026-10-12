# Methodology

This document describes how a bedrock-bench run is carried out and judged: the rules every run follows, the phases and
measurement windows, how the server and bots are placed on the machine, how repeats are aggregated, and when a run
counts. Metric formulas are in [METRICS.md](METRICS.md); the workloads are in [WORKLOADS.md](WORKLOADS.md).

## Rules

Each rule is enforced by code unless marked "documented".

| Rule | Statement | Enforcement |
|---|---|---|
| R1 | One scenario file drives every arm in a matrix. No per-arm phase overrides. | The config loader rejects scenario or phase keys in an arm. |
| R2 | One bot source with identical flags for every arm. Only allowlisted identity flags (`-fake-xuid`) may differ. The protocol 1001 build for PocketMine-MP is flagged `bot_protocol_differs`. | Adapter contract test; report flag. |
| R3 | Identical server CPU set, bot CPU set, priority and JVM heap for every arm. | The runner refuses mixed placement; the report refuses mixed machine profiles. |
| R4 | One measured run at a time, with continuous interference sampling. | Machine lock, idle gate, per-window foreign-load gate. |
| R5 | A fresh server copy and a fresh (or validated template) world for every run. Nothing is edited in place. | Run directory layout, install-tree hash. |
| R6 | Every config change from a server's defaults is recorded with a category: `harness`, `parity`, `rescue` or `nonvanilla`. `rescue` and `nonvanilla` always produce a footnote. | Adapters return an override list from `prepare()`. |
| R7 | No interventions (nudges, re-teleports, commands other than the tick probe) inside measured windows. | `interventions_in_measured` gate. |
| R8 | TPS comes only from a monotonic counter that advances once per executed tick. World time and day time are rejected. | Adapters declare the probe monotonic; the probe must advance during idle. |
| R9 | Crashes and join failures are outcomes. They are never dropped or silently rerun. | Run status model below. |
| R10 | Every cost table sits next to a work table and the parity rows for the features the scenario exercises. | Report layout. |

## Run lifecycle

```
bbench run matrices/X.toml
  runner: take the machine lock, run doctor checks and the idle gate,
          then for each (arm, repeat) in rotated order start one `bbench run-one` subprocess
  run-one:
    fetch      verified, immutable install from the cache
    prepare    fresh server copy, rendered configs, categorised overrides, world reset or template
    launch     suspended, pinned, prioritised, then resumed (Windows); affinity set before exec (Linux)
    console    log pump, tick probe, samplers, death watch
    phases     idle -> join -> settle -> spread -> steady -> [chaos]
    teardown   bots quit, server stop, crash collection
    results    run.json, raw series, metrics.json, status, exit code
  runner: summary.json and report.md for the matrix directory
```

Each run is a separate process, so a crash or a hang in one run cannot leak state into the next. Run directories are
never overwritten. `run.partial.json` is written after every phase, so an interrupted run keeps what it measured.

## Phases

| Phase | Starts | Ends | What happens |
|---|---|---|---|
| boot | launch | the adapter's ready line | Version banner must match the arm. Cap 600 s. |
| idle | ready + 10 s | + 20 s | No bots. Thread census and idle baseline. The tick probe must advance. |
| join | `bots_start` | `alljoined` | Bots dial with a 300 ms stagger. Each bot gets its effects and teleport once the server says it is in the world. Cap 300 s. |
| settle | `alljoined` | `settled` | Views fill. Re-teleports and nudges are allowed here and only here. |
| spread | after `settled` | | Effects re-sent to everyone, positions read back, spread validated. |
| steady | `settled` + 15 s | + 90 s | Only the tick probe is sent. Headline window. |
| chaos | after steady | + 900 s | Chaos scenarios only: seeded actions from a pre-generated plan. |
| teardown | | | Bots get `quit` (a signal after 10 s), the server gets its stop command (process tree killed after 180 s), crash files are collected. |

`pre_bots_s` (default 0) adds a pause between idle and the first dial. The reproduction of the legacy runs can set it to
12 s to match the legacy harness.

### Measurement windows

| Window | Interval | Used for |
|---|---|---|
| `idle` | `[idle0, idle1]` | baseline CPU and memory, thread census, probe advance check |
| `join` | `[bots_start, alljoined]` | join latency |
| `settle` | `[alljoined, settled]` | diagnostics |
| `load` | `[bots_start, settled]` | dip depth, ticks lost, recovery, chunk throughput |
| `steady` | `[steady0, steady1]` | headline TPS, tick-thread CPU, process cores, memory, work counters |
| `chaos` | `[chaos0, chaos1]` | chaos TPS by tick-speed regime |

`load` keeps the legacy definition. Because settle cannot end before 300 s, `load` is at least 300 s long, which is why
the settle duration itself is not reported as a speed metric.

### Settle rule

The settle rule is kept from the legacy harness so that published figures stay comparable.

- It is evaluated every 5 s from the bots' `stat` and `progress` events. Data older than 5 s is not used.
- `rate` is the growth of the total number of unique in-view chunks across all bots over the last 60 s, per second.
- Settle ends when at least 300 s have passed since `alljoined` and `rate / n <= 0.1` chunks per bot per second.
- Otherwise it ends at 900 s and the run is flagged `settle_capped`.
- At `settled`, if fewer than 95% of bots hold at least half the median bot's in-view chunk count, the run is flagged
  `views_incomplete`. Non-headline scenarios can make this an end condition with `min_view_frac`.

A chunk-rate threshold alone is not a sign that loading is done: a saturated server delivers chunks slowly because its
tick thread is full. That is why the rule needs a floor of 300 s and why `views_incomplete` exists.

### Interventions

Two interventions keep a run usable when a teleport is lost or a bot hovers above unloaded terrain. Both are bounded,
counted per bot, recorded in `run.json`, and never happen after `settled`.

- **Re-teleport:** a bot with no confirmed teleport 20 s after its placement commands gets them again, up to 3 times
  (`retp`).
- **Nudge:** after 60 s of settle, a bot that the server reports above y = 290 with fewer than 30 unique chunks is moved
  16 blocks sideways at y = 300, up to 2 times (`nudges`). Why bots occasionally hover is an open question; a
  terrain-following walk mode is planned.

### Spread validity

After settle the harness reads every player's position from the server (`querytarget @a` on BDS-family servers, a
helper command elsewhere, bot-reported positions as a labelled fallback) and maps each to its nearest grid cell. The
spread is valid when every joined bot occupies its own cell (`cells_occupied == joined` and `max_per_cell == 1`). The
worst offset from a cell centre is reported, with a warning above 200 blocks. Unknown positions make the run invalid;
they are never read as zero players.

## Tick probe

Every 5 s the harness sends the adapter's tick command (`time query gametime` on BDS-family servers, a helper command
on servers whose own command does not print a per-tick counter) through a priority lane of the console writer, ahead
of any queued command. Each reply is stamped with the harness monotonic clock. TPS over a window is the counter
difference divided by the time difference of the replies at the window edges.

- Adapters declare whether their counter is monotonic. A server without such a counter reports TPS as unavailable.
- The counter must advance during idle, or the run is invalid. This catches servers that only tick worlds with players.
- Probe latency (reply time minus send time) is recorded. A p99 above 1000 ms in steady makes the run invalid.
- A gap of more than 15 s between replies inside a measured window makes the run invalid.
- 5 s keeps the lowest-5 s and lowest-15 s figures comparable with the legacy numbers. Rates over shorter spans are
  dominated by reply jitter and are never published on their own.

## Sampling

| Sampler | Rate | Source | Output |
|---|---|---|---|
| threads | 1 Hz | per-thread CPU time and names (Windows thread times, Linux `/proc/<pid>/task/*`) | `series/threads.csv.gz` |
| tick thread | 10 Hz | CPU time of the thread chosen by the adapter's tick-thread rule | `series/tick.csv.gz` |
| process | 1 Hz | process and process-tree CPU, memory, threads, handles | `series/process.csv.gz` |
| machine | 1 Hz | per-CPU busy time; foreign load = machine busy minus the server, bot and harness trees | `series/machine.csv.gz` |
| foreign top | every 10 s | the busiest foreign processes | `series/foreign_top.jsonl` |

Exited threads keep their last CPU value, and threads created inside a window count from zero, so short-lived workers
are not lost. Process CPU comes from process counters, which include exited threads. The samplers are cheap by design;
the harness's own CPU is reported as `harness_cores`.

## Placement and isolation

The machine profile (`machines/*.toml`) names three CPU sets and a priority:

- **Server CPUs.** `auto` picks the performance class on hybrid CPUs (Windows CPU-set efficiency class, Linux
  `cpu_core` sysfs). Otherwise an explicit list such as `0-15`.
- **Bot CPUs.** `auto` takes the rest. At least 4 logical CPUs, or `doctor` refuses.
- **Harness CPUs.** By default the bot set.
- **Priority.** `normal` by default; the `endstone-0.11.14` and `endstone-0.11.14-csg` matrices use `above_normal` to
  match the legacy runs. Every arm of a matrix gets the same priority.

On Windows the server starts suspended, gets its affinity and priority, and only then resumes, so thread pools sized
at startup (JVM, BDS) see the restricted set. On Linux affinity is set before `exec`. In Docker the container gets
`--cpuset-cpus`. The affinity of the server and every one of its threads is checked at ready, at the start of steady
and at the end of steady (`placement_match`). The placement is recorded as `physical` or, in VMs, WSL2 and Docker
Desktop, `vcpu`.

Interference is handled in three layers:

1. **Machine lock.** One measured run per machine at a time. The lock is an operating-system file lock that is released
   when the process dies.
2. **Idle gate.** Before each run the harness samples the machine for 15 s and refuses to start under foreign load.
3. **Window gate.** Foreign load is sampled every second. A run whose measured windows exceed the profile's limits
   (default: mean foreign load 0.25 cores, 10 s peak 1.5 cores, 0.25 cores on the server's CPUs) is invalid. The
   busiest foreign processes are recorded so the cause can be found.

The bots share the host with the server. They are pinned away from it, and a run whose bot CPUs are more than 85%
busy is invalid (`bot_saturated`), because a starved bot under-reports what the server delivers.

## Repeats and statistics

- **Repeats.** At least 3 per cell for publication, 5 recommended. `bbench report --publish` refuses fewer than 3 `ok`
  runs per cell unless every run of the cell failed.
- **Order.** `rotate`: block k runs the arms rotated by k, so no arm always runs first or last. 60 s cooldown and the
  idle gate between runs.
- **Aggregation.** Median, minimum, maximum and every value, plus `n_ok/n_total`. `server_failed` runs appear in the
  status column and never in a median.
- **Comparison.** "A better than B" is printed only when every run of A beats every run of B. With 3 runs each, that
  happens by chance with probability 1/20 under no difference. Otherwise the report prints `~`. Ratios are printed as
  the median ratio with the range `[min A / max B, max A / min B]`.
- **No outlier removal.** A run is either invalid by a gate (and rerun once) or kept.
- **Percentiles** use the nearest rank: `sorted[round(q * (n - 1))]`. Every statistic reports its `n`. The 5th
  percentile is omitted when `n < 20`.

## Validity gates and run status

| Gate | Fails the run as | Rule |
|---|---|---|
| `harness_ok` | invalid | no harness exception; raw files complete |
| `probe_monotonic` | invalid | counter declared monotonic, never decreasing, advanced during idle |
| `probe_coverage` | invalid | no gap over 15 s between probe replies in measured windows |
| `probe_lagging` | invalid | probe latency p99 at most 1000 ms in steady |
| `foreign_load` | invalid | machine-profile limits in every measured window |
| `placement_match` | invalid | server and all its threads on the planned CPUs at ready and around steady |
| `decode_errors` | invalid | the bots decoded every packet |
| `bot_saturated` | invalid | bot CPU busy fraction at most 0.85 |
| `spread` | invalid | one bot per cell, positions known |
| `csg_matches` | invalid | effective client-side generation equals the scenario's setting (servers that support it) |
| `version_match` | invalid | the server's banner version equals the arm version |
| `world_fresh` | invalid | world directory absent before launch, or template hash matches |
| `config_readback` | invalid | parity keys read back after the run equal what was written |
| `interventions_in_measured` | invalid | no interventions in measured windows |
| `server_alive` | server_failed | the server did not exit before teardown and exited cleanly after stop |
| `joined_all` | server_failed | every bot joined (reported as joined/n) |
| `dropped` | warning | no bot dropped outside chaos |

Flags (`settle_capped`, `views_incomplete`, `chaos_overrun`, `parity_mismatch`, `bot_protocol_differs`,
`terrain_mismatch`, `different_bds_build`, `known_issue`, `unpublished_artifact`) never change the status but always
appear next to the numbers.

**Status:** `invalid` if any invalid gate fails, else `server_failed` if any outcome gate fails, else `ok`. `invalid`
means the harness or the environment failed, so the run is rerun once. `server_failed` is a result about the server and
is never rerun.

When the server dies or exits nonzero, the harness copies the last 200 log lines and every file modified since the run
started under the adapter's crash paths into `crash/`.

**Exit codes** come from the recorded statuses, so a run in which the server died can never exit 0:

| Code | Meaning |
|---|---|
| 0 | every run `ok` |
| 2 | usage or config error |
| 3 | an environment gate refused to start (lock held, idle gate, doctor check) |
| 4 | at least one `server_failed`, no `invalid` |
| 5 | at least one `invalid` after reruns |
| 130 | interrupted |

## Limitations

Each report repeats the limitations that apply to its numbers. The general ones:

- **One machine.** Absolute numbers do not transfer between machines. Ratios between arms on one machine transfer
  better, but not exactly: a server that is saturated on one machine may not be on another.
- **Bots are not players.** Every bot behaves the same way: it walks a circle, never changes height on its own, never
  builds, and acknowledges nothing it does not have to. Its client-side cache always misses, so every chunk is sent in
  full. That is a worst case, applied equally to every server.
- **Movement corrections.** A bot claims flat walking over real terrain, so servers that validate movement send
  corrections that a real client would not cause. They are counted and reported as work.
- **Loopback.** All bots connect from 127.0.0.1 without encryption. Per-IP flood guards are raised per adapter and
  listed as overrides; encryption cost is excluded.
- **Shared host.** Bots run on the same machine on separate CPUs. They still share memory bandwidth, the last-level
  cache and the power budget with the server.
- **Fresh worlds.** On a fresh world, terrain generation dominates the load phase. `spread100-pregen` separates
  delivery from generation. Mob-dense and redstone-dense workloads are not covered yet.
- **Tick-thread CPU is not MSPT.** It excludes time the tick thread spends blocked. Wall-clock MSPT is reported only
  when the server is saturated, where it equals 1000 / TPS ([ADR 0003](adr/0003-tick-cpu-is-not-mspt.md)).
- **Virtual CPUs.** Under WSL2, Docker Desktop or a VM, pinning applies to virtual CPUs, which the host may move between
  physical cores. Such runs are marked `vcpu` and carry a warning. Published Linux numbers should come from bare metal.
- **Memory** is private bytes on Windows and RSS on Linux. The two are never compared in one column, and JVM servers
  show their fixed heap.
- **Chaos is a robustness check.** It is never ranked.
