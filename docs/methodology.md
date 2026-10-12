# Methodology

How bedrock-bench runs a benchmark and judges the result. Bot counts, phase lengths and thresholds live in the files
under [`configs/`](../configs/) and in the code defaults those files override. This page names the keys and explains
the rules. Metric formulas and gate thresholds are in [metrics.md](metrics.md).

## Rules

Code and error messages cite these rules by number.

| Rule | Statement | Enforcement |
|---|---|---|
| R1 | One scenario file drives every arm in a matrix. No per-arm phase overrides. | The config loader rejects scenario or phase keys in an arm. |
| R2 | One bot source with identical flags for every arm. Only allowlisted identity flags (`-fake-xuid`) may differ. The protocol 1001 build for PocketMine-MP is flagged `bot_protocol_differs`. | Adapter contract test; report flag. |
| R3 | Identical server CPU set, bot CPU set, priority and JVM heap for every arm. | The runner refuses mixed placement; the report refuses mixed machine profiles. |
| R4 | One measured run at a time, with continuous interference sampling. | Machine lock, idle gate, per-window foreign-load gate. |
| R5 | A fresh server copy and a fresh world for every run. Nothing is edited in place. | Run directory layout, install-tree hash. |
| R6 | Every config change from a server's defaults is recorded with a category: `harness`, `parity`, `rescue` or `nonvanilla`. `rescue` and `nonvanilla` always produce a footnote. | Adapters return an override list from `prepare()`. |
| R7 | No interventions (nudges, re-teleports, commands other than the tick probe) inside measured windows. | `interventions_in_measured` gate. |
| R8 | TPS comes only from a monotonic counter that advances once per executed tick. World time and day time are rejected. | Adapters declare the probe monotonic; the probe must advance during idle. |
| R9 | Crashes and join failures are outcomes. They are never dropped or silently rerun. | Run status model ([below](#statistics-and-run-status)). |
| R10 | Every cost table sits next to a work table and the parity rows for the features the scenario exercises. | Report layout. |

## Workload

A workload is a scenario file in [`configs/scenarios/`](../configs/scenarios/). One scenario drives every arm of a
matrix (R1). Its sha256 covers the fully resolved content, after `extends` and defaults, and every run stores both the
hash and the resolved content, so runs of different scenario versions are never mixed. Phase defaults are `PHASE_DEFS`
in `bedrock_bench/config.py`.

| Scenario | Purpose |
|---|---|
| `smoke` | Checks one adapter end to end with a few bots (`bbench smoke`). Every command the harness sends must be accepted. |
| `spread100` | The headline for every server, Endstone included. Client-side generation off. |
| `spread100-csg` | `spread100` with client-side generation on. BDS-hosted servers only, never in a cross-server table. |

Each scenario lists the features whose parity rows the report shows (`exercises`) and the features that must match for
efficiency metrics to be comparable (`requires`).

**Grid.** Bot `i` goes to cell `(origin + (i % columns) * spacing, origin + (i // columns) * spacing)` from the
scenario's `[grid]`. The loader refuses a spacing that is not larger than two view distances in blocks, so no two bots
share a chunk. Spread players are the worst case for a server: each one needs its own terrain generated, loaded, ticked
and sent. Clustered players share chunks and flatter the server.

**Placement.** Once a bot reports `spawned` and the server prints the adapter's player-in-world line, the harness gives
the bot protective effects, so it cannot die from falls, fire, water or hunger, and teleports it to its cell at height
`grid.y`. The server generates the area while the bot falls. `spreadplayers` is never used, because it puts players at
unpredictable heights in ungenerated terrain.

**Bots.** The same Go bot runs against every server ([bots/README.md](../bots/README.md#flags)). It logs in offline
over RakNet from 127.0.0.1 with a deterministic identity, walks a circle, reports every cached chunk as a miss so the
server always sends full chunks, and counts unique chunks within its current view. A decode error makes the run
invalid, because the bot did not see everything the server sent.

**World.** The world is absent before launch and generated during the run (`world_fresh` checks it).

## Run phases

```
bbench run <matrix>
  runner: take the machine lock, run the doctor checks and the idle gate,
          then start one `bbench run-one` subprocess per (arm, repeat) in rotated order
  run-one:
    fetch      verified, immutable install from the cache
    prepare    fresh server copy, rendered configs, categorised overrides, fresh world
    launch     pinned and prioritised before the first server thread runs
    console    log pump, tick probe, samplers, death watch
    phases     boot -> idle -> join -> settle -> spread -> steady -> teardown
    results    run.json, raw series, metrics.json, status, exit code
  runner: summary.json and report.md for the matrix directory
```

Each run is its own process, so a crash or a hang cannot leak state into the next. `run.partial.json` is rewritten
after every phase, so an interrupted run keeps what it measured.

| Phase | From | To | What happens |
|---|---|---|---|
| boot | launch | the adapter's ready line | The version banner must match the arm. |
| idle | ready + `delay_s` | + `duration_s` | No bots. Thread census and idle baseline; the tick probe must advance. |
| join | `bots_start` | `alljoined` | Bots dial `bots.stagger_ms` apart and are placed as they enter the world. |
| settle | `alljoined` | `settled` | Views fill. Interventions are allowed here and only here. |
| spread | `settled` | | Effects sent to everyone again, positions read back from the server, spread checked. |
| steady | `settled` + `delay_s` | + `duration_s` | Only the tick probe is sent. The headline window. |
| teardown | | | Bots get `quit`, then a signal; the server gets its stop command, then its process tree is killed; crash files are collected. |

`join.pre_bots_s` adds a pause between idle and the first dial. It is 0 unless a scenario sets it.

| Window | Interval | Used for |
|---|---|---|
| `idle` | `[idle0, idle1]` | baseline CPU and memory, thread census, probe advance |
| `join` | `[bots_start, alljoined]` | join latency |
| `settle` | `[alljoined, settled]` | diagnostics |
| `load` | `[bots_start, settled]` | dip depth, ticks lost, recovery, chunk throughput |
| `steady` | `[steady0, steady1]` | headline TPS, tick-thread CPU, process cores, memory, work counters |

**Settle.** The rule is evaluated every `eval_interval_s` from the bots' `progress` events, never on data older than
`max_data_age_s`. The rate is the growth of all bots' unique chunks over the last `rate_window_s`.
Settle ends once `min_s` has passed since `alljoined` and the rate per bot is at most `rate_per_bot`, or at `cap_s`
with the flag `settle_capped`. If, at `settled`, fewer than `view_complete_frac` of the bots hold `view_median_frac` of
the median bot's in-view count, the run is flagged `views_incomplete`. `min_view_frac` can make that an end condition.
A low chunk rate alone does not mean loading is done, since a saturated server delivers chunks slowly because its tick
thread is full. That is why there is a floor, and why the settle duration is never a speed metric.

**Interventions.** Both are bounded, counted per bot, recorded in `run.json` and never happen after `settled` (R7).

- Re-teleport: a bot with no confirmed teleport `retp_after_s` after its placement gets it again, up to `retp_max`
  times.
- Nudge: after `nudge_after_s` of settle, a bot still above `nudge_min_y` with fewer than `nudge_max_uniq` unique
  chunks is teleported over its cell again, `nudge_step` blocks aside on alternate tries, up to `nudge_max` times. Why
  bots sometimes hover is an open question.

**Spread.** After settle the harness reads every player's position from the server (`querytarget @a` on BDS-hosted
servers, a helper command elsewhere, bot-reported positions as a labelled fallback) and maps each to its nearest cell.
The spread is valid when every joined bot has its own cell (`cells_occupied == joined` and `max_per_cell == 1`). The
worst offset from a cell is reported, with a warning above `grid.warn_offset_blocks`. Unknown positions make the run
invalid. They are never read as zero players.

**Tick probe.** Every `probe.interval_s` the harness sends the adapter's tick command through a priority lane of the
console writer, ahead of any queued command, and stamps each reply with its monotonic clock. TPS over a window is the
counter difference divided by the reply-time difference at the window's edges (R8). The counter must advance during
idle, which catches servers that only tick worlds with players. Probe gaps and probe latency are gated
([metrics.md](metrics.md#validity-gates)). Rates over shorter spans are dominated by reply jitter and are never
published alone.

| Sampler | Rate | Source |
|---|---|---|
| threads | 1 Hz | CPU time and name of every server thread |
| tick thread | 10 Hz | CPU time of the thread the adapter's tick-thread rule picks |
| process | 1 Hz | process and process-tree CPU, memory, threads, handles |
| machine | 1 Hz | per-CPU busy time; foreign load is machine busy time minus the server, bot and harness trees |
| foreign top | every 10 s | the busiest foreign processes |

Threads created inside a window count from zero and exited threads keep their last value. Process CPU comes from
process counters, which include exited threads. The harness's own CPU is reported as `harness_cores`.

## Metrics

[metrics.md](metrics.md) is generated from `bedrock_bench/metrics/registry.py` and lists every metric with its unit,
window, inputs and formula. Metrics are pure functions of a run's raw files: `bbench analyze` recomputes
`metrics.json` byte for byte under the same analysis version.

- **Tick rate.** TPS from the probe counter only (R8): steady TPS, and while views load the lowest 5 s and 15 s rates,
  ticks lost, time below 19.5 TPS and recovery time.
- **Headroom.** Tick-thread CPU per executed tick and the share of time the tick thread is busy or saturated. This is
  not MSPT ([below](#tick-thread-cpu-is-not-mspt)).
- **CPU and memory.** Process cores is the cross-server CPU metric. Memory definitions differ by operating system.
- **View load and work.** Time to fill each bot's view, chunks per player, movement corrections, actor moves, block
  updates, locator packets and the packet mix: what the server did for its CPU time.
- **Efficiency.** CPU per player-tick and similar ratios, comparable only between arms with equal parity on the
  scenario's `requires` list.
- **Health.** Join outcomes, drops, deaths, decode errors and log counts, and the validity gates.

Every metric has an availability: `measured`, `not_applicable` (the server or scenario has no such quantity) or
`unavailable` (it should exist but could not be computed; a reason says why).

## Fairness and feature parity

A comparison between servers is fair only if every server gets the same machine, load and chance, and every difference
that remains is visible.

- One harness for every server. Reports read only `metrics.json`, which the same code computes for every server.
- One scenario, one machine profile and one bot flag set per table (R1 to R3), checked by hash.
- The scenario states gameplay settings once (`[server]`: view and simulation distance, seed, difficulty, game mode,
  player cap, autosave, client-side generation). Each adapter maps them onto its server's own keys, listed in the
  `[config]` table of `servers/<server>/server.toml` (BDS-hosted servers use the BDS one), and `run.json`
  records every change. Difficulty is easy on every server, the BDS default, except Chorus, which is always peaceful
  and has no simulation distance ([its README](../servers/chorus/README.md#caveats)).
- Equal resources: one fixed heap for every JVM server (`-Xms` equals `-Xmx`, from the machine profile), PocketMine-MP
  and Altay async workers equal to the server CPU count, and BDS `max-threads` left at its default and recorded.
- Telemetry, crash upload, usage statistics and auto-update are off where a switch exists.
- All bots connect from 127.0.0.1, which trips per-IP flood guards real players never hit. Adapters raise them as
  `harness` overrides, except LeviLamina's, which stays at its default, and Nukkit-MOT's, which exempts the machine's
  own addresses. `dropped == 0` is checked.
- A world not built by the vanilla generator is flagged `terrain_mismatch`.

**Override categories.** Every value an adapter writes that differs from the server's default becomes an
`Override(category, file, key, default, value, reason)` in `run.json` (R6). The values and reasons live in each
server's `server.toml`.

| Category | Meaning | Footnote |
|---|---|---|
| `harness` | Needed to drive the server at all, without changing the simulation: offline mode, ports, console, helper plugin, raised per-IP limits, telemetry off. | no |
| `parity` | Aligns the workload across servers: distances, seed, difficulty, player cap, client-side generation, heap, fixed tick rate. | no |
| `rescue` | The default config fails the workload; the change lets it run. | yes, stating what the default config does |
| `nonvanilla` | Changes gameplay to save work. | yes |

**Variants.** A variant selects another override set for one server, for example a `stock` variant without a rescue
override. It is appended to the arm id with `+`, as in `levilamina@26.51.6+levioptimize-all`, and shown in every table.

**Same-build baseline.** An arm that hosts BDS is compared with the vanilla arm of the exact BDS build it hosts. When
the matrix has no such arm, the arm is flagged `different_bds_build`.

**Confounders are disclosed, never subtracted.** The allocator, the JVM heap and known defects of a release are recorded
in `run.json`.

**Feature parity.** Two servers can run the same workload and still do different amounts of work. Each server declares,
in the `[features]` table of its `server.toml`, how each feature compares with vanilla BDS, per version range:

```toml
[features.range."5.44"]   # matches 5.44 and 5.44.x; the longest matching prefix wins, "*" matches any version
fidelity = "V1"
"player.physics" = { class = "absent", observed_by = "airborne_frac_steady", note = "bots keep hovering at y=300" }
"terrain.structures" = { class = "partial", evidence = "https://example.org/docs/generator" }
```

A version that matches no range has no row and reads as `unknown`. `run.json` records the matching range, the class of
every exercised feature and the hash of the `[features]` table.

| Fidelity | Meaning |
|---|---|
| `V0` | Vanilla code paths: Mojang's server, or Mojang's implementation unchanged. |
| `V1` | Equivalent behaviour: some paths are replaced with faster ones that claim the same observable results. |
| `V2` | Changed gameplay: the build skips or alters work on purpose to go faster. |

| Class | Meaning |
|---|---|
| `vanilla` | Implemented, with the same observable behaviour as vanilla BDS. |
| `partial` | Implemented, with parts missing or behaving differently. |
| `altered` | Changed on purpose to save work. Needs a `note` saying what changes for players. |
| `absent` | Not implemented. |
| `unknown` | Not assessed. Never allowed in a published table for a feature the scenario exercises. |

**Evidence.** Every class other than `unknown` needs evidence, and the loader refuses a cell without it: a public link
(`evidence`) to source at a pinned commit or tag, documentation or a changelog, an observing metric (`observed_by`), or
both. Decompiled code, reverse-engineering notes, leaked material and anything under an NDA are never evidence. A
project's claims about itself, such as its own test suite, count as evidence for its own declaration only; the harness
never presents them as its own measurement. Where a counter observes a feature, the report prints the declared class
next to the observed value and flags `parity_mismatch` when they disagree.

**Feature keys.** A new key goes into this table and into `FEATURE_KEYS` in `bedrock_bench/model.py` in the same change.

| Key | Vanilla behaviour | Observed by |
|---|---|---|
| `terrain.vanilla_generator` | Overworld terrain from Mojang's generator, identical to BDS for the same seed. | |
| `terrain.any_generator` | Terrain is generated at all (not only a flat or void world). | |
| `terrain.structures` | Villages, temples and other structures are placed during generation. | |
| `chunks.client_side_generation` | Clients that support it generate terrain from the seed; the server sends less. | `csg_effective` |
| `chunks.subchunk_requests` | Chunk columns are delivered through sub-chunk requests. | |
| `entity.mob_spawning` | Mobs spawn naturally around players. | |
| `entity.mob_ai` | Mobs path-find, target and act. | |
| `entity.collision_push` | Entities push each other apart on collision. | |
| `entity.item_entities` | Dropped items exist as entities, merge and despawn. | |
| `player.server_authoritative_movement` | The server checks player movement and corrects the client when it disagrees. | `corrections_per_bot_s` |
| `player.physics` | Players fall, collide with blocks and land; a client cannot hover by claiming to. | `airborne_frac_steady` |
| `player.hunger_damage` | Hunger, fall damage and other player damage apply. | |
| `player.locator_bar` | Each player receives the positions of other players for the locator bar. | `locator_pkts_per_bot_s` |
| `block.redstone` | Redstone components update and propagate signals. | |
| `block.random_ticks` | Random block ticks at the `randomtickspeed` game rule (crops, leaves, fire, ...). | |
| `block.fluids` | Water and lava flow. | |
| `block.falling` | Sand, gravel and other gravity blocks fall. | |
| `world.lighting` | Sky and block light are computed. | |
| `world.dimensions` | The Nether and the End exist and can be travelled to. | |
| `world.autosave` | The world is saved to disk periodically while running. | |
| `commands.vanilla_set` | The vanilla command set (`tp`, `effect`, `gamerule`, `querytarget`, ...) is available. | |
| `net.encryption` | Online-mode logins and packet encryption. Benchmarks run offline, so this is never exercised. | |

## Placement and interference

The machine profile is a TOML file passed with `--machine`. Without one, built-in defaults apply. An annotated example
is in [get_started.md](get_started.md#machine-profile). It names three CPU sets and a priority:

- **Server CPUs.** `auto` gives the server every CPU of the fastest class on a hybrid CPU. Otherwise the bots get whole
  physical cores, slowest first, until they have a quarter of the cores, and the server gets the rest. SMT siblings
  stay together. An explicit list such as `0-15` is used as is.
- **Bot CPUs.** `auto` takes the rest. Fewer than 4 logical CPUs fails `bbench doctor`, because a starved bot shows up
  as server slowness.
- **Harness CPUs.** The bot set, unless the profile says otherwise.
- **Priority.** `normal` unless the matrix sets `priority_override`. Every arm of a matrix gets the same priority, and
  it is recorded.

On Windows the server starts suspended, gets its affinity and priority, and only then resumes, so thread pools sized at
startup (JVM, BDS) see the restricted set. On Linux affinity and priority are set between `fork` and `exec`. The
affinity of the server and every one of its threads is checked at `ready`, `steady0` and `steady1`
(`placement_match`). Placement is recorded as `physical`, or as `vcpu` on a virtual machine or WSL2, where pinning
applies to virtual CPUs the host may move between cores.

Each server runs once per experiment, on one machine and one platform. A report refuses runs from different machines
(the fingerprint includes the operating system family) or different machine profiles (R3).

Interference is handled in three layers (R4):

1. **Machine lock.** One measured run per machine at a time. The lock is an operating-system file lock, released when
   the process dies.
2. **Idle gate.** Before each run the harness samples the machine for `gates.idle_check_s` and refuses to start under
   foreign load.
3. **Window gate.** Foreign load is sampled every second. A run whose measured windows exceed the profile's `[gates]`
   limits (mean, 10 s peak, and load on the server's CPUs) is invalid. The busiest foreign processes are recorded so
   the cause can be found.

The bots share the host with the server, on separate CPUs. A run whose bot CPUs are busier than `bot_cpu_frac_max` is
invalid (`bot_saturated`), because a starved bot under-reports what the server delivers. How to prepare a machine is in
[get_started.md](get_started.md#preparing-the-machine).

## Statistics and run status

- **Repeats.** The matrix sets `repeats`. Publication needs at least 3 `ok` runs per cell, and 5 are better.
- **Order.** `rotate`: block k runs the arms rotated by k, so no arm always runs first or last, with `cooldown_s` and
  the idle gate between runs.
- **Aggregation.** Median, minimum, maximum and every value of the `ok` runs, with `n_ok/n_total`. `server_failed` runs
  count in `n_total` and the status column, never in a median.
- **Comparison.** "A better than B" is printed only when every run of A beats every run of B and the chance of that
  with no real difference, `1 / C(n_A + n_B, n_A)`, is at most 0.05. With 3 runs each it is 1/20, so arms with fewer
  runs never separate. Otherwise the report prints `~`. Ratios are the median ratio with the range
  `[min A / max B, max A / min B]`.
- **No outlier removal.** A run is either invalid by a gate or kept.
- **Comparability.** Runs with different scenario hashes, machines, machine profiles or analysis versions never share a
  table; the report refuses them.

**Status.** Each gate in [metrics.md](metrics.md#validity-gates) fails a run as `invalid` or `server_failed`, or adds a
warning. The status is `invalid` if any invalid gate fails, else `server_failed` if an outcome gate fails, else `ok`.
`invalid` means the harness or the machine failed, and the run is rerun once (`rerun_invalid`). `server_failed` is a
result about the server and is never rerun (R9). Gates on a window the run never reached are skipped, so a server that
dies early is `server_failed`, not invalid. Exit codes come from the recorded statuses, so a run in which the server
died never exits 0 ([get_started.md](get_started.md#reading-the-output)).

Flags never change the status but always appear next to the numbers: `settle_capped`, `views_incomplete`,
`parity_mismatch`, `bot_protocol_differs`, `terrain_mismatch`, `different_bds_build` and `unpublished_artifact`.

When the server dies or exits nonzero, the harness copies the last `teardown.crash_log_lines` log lines, and every file
modified since the run started under the adapter's crash paths, into `crash/`.

## Reading a report

`report.md` opens with the conditions (harness commit, machine, placement, scenario hash, bot build, repeats), then
shows the parity, cost, work and efficiency tables and the footnotes for each scenario.

1. **20 TPS is a ceiling, not a score.** A server never runs faster than 20 TPS, so two servers that both hold it can
   be far apart. Compare them on headroom: `tick_cpu_ms_per_tick` against the 50 ms budget of a tick, `tick_busy_frac`
   and `tick_saturated_frac`. Below 20 TPS the tick thread is saturated, TPS itself is the measure, and
   `mspt_saturated` is the real time per tick. Go servers have no single tick thread, so compare process cores.
2. **Parity before cost.** Arms that are `vanilla` on every exercised feature are under "Full vanilla fidelity", the
   rest under "Reduced fidelity", each with a "does less than baseline" list. A server that skips physics, mob AI or
   terrain generation is cheaper for that reason. The two sections are never ranked against each other.
3. **The same workload is not the same work.** Servers that validate movement send corrections, servers with physics
   make the bots land, and servers with neither leave them hovering at the teleport height, which the y distribution
   shows. The work table shows what each server did for its CPU time. Efficiency metrics are comparable only between
   arms with the same parity on the scenario's `requires` list.
4. **Ranges, not points.** `~` means "not separated on this evidence", not "equal". Run-to-run noise on a desktop is
   large enough that a small gap between two medians usually means nothing.
5. **Failures are results.** A crash, a watchdog shutdown or a server that cannot take every player is
   `server_failed`, shown as `k/n ok` with a footnote. A `rescue` footnote states what an out-of-the-box install would
   do. `invalid` runs say something about the harness or the machine, not the server.
6. **Pick the lens for the use case.** For a survival server with many spread players: steady TPS, tick-thread CPU per
   tick, the lowest TPS while views load and view-load time, under full vanilla fidelity. For a lobby or minigame
   server: a stable tick and low cost per player; reduced fidelity may be fine if the "does less" list does not matter
   for the game. For capacity planning: headroom, not TPS, and only for this machine.

These look comparable but are not:

- memory across operating systems or runtimes: the definitions differ, and a JVM server's memory is mostly its fixed
  heap;
- log error counts across servers, since each server logs differently;
- join time, measured on the bot's clock from dial to spawn, before any chunk arrives;
- settle duration, which has a floor; use the view-load metrics instead.

## Results files

Every run leaves a self-contained directory:

```
runs/<matrix>/
  <run_id>/
    run.json             provenance, config, marks, overrides, interventions, spread, status
                         (run.partial.json while the run is in progress)
    metrics.json         computed from the raw files; regenerated by `bbench analyze`
    server.log.gz        server console output
    commands.jsonl.gz    every console write
    bots.jsonl.gz        raw bot events
    bots.stderr.log      the bot's standard error, kept only when it is not empty
    series/              probe.csv, threads.csv.gz, tick.csv.gz, process.csv.gz, machine.csv.gz, foreign_top.jsonl
    config/written/      rendered configs as written before launch
    config/after/        the same files as found after shutdown
    crash/               log tail and crash files, when the server died or exited nonzero
  summary.json           aggregate over repeats
  report.md              the rendered report
```

The run id is `<UTC yyyymmddThhmmssZ>-<arm>-<scenario>-r<repeat>-<4 hex>`, for example
`20261012T135800Z-endstone@0.11.14-spread100-r1-3fa2`. Run directories are never overwritten or deleted.

**Raw formats.** `server.log.gz` has one line per console line, `<mono_ns>\t<epoch_ms>\t<line>`, with ANSI escapes
stripped. `commands.jsonl.gz` has one object per console write with its enqueue and send times, lane and origin.
`bots.jsonl.gz` has the bot's events ([bots/README.md](../bots/README.md#events)), each with `rx_ns`, the time the
harness read it. Every CSV starts with a header row, and `bedrock_bench/sampling/series.py` lists the columns. Memory
is in MiB (2^20 bytes).

**Time.** Every harness timestamp is monotonic nanoseconds (`*_ns`) from one clock, taken when the harness reads the
data. `run.json` stores one origin pair taken together at the start, `time.origin_mono_ns` and `time.origin_epoch_ms`,
which maps epoch-stamped sources such as bot events onto the monotonic timeline. `time.drift_ms` records how far the two
clocks drifted apart during the run. Phase marks are seconds since the origin.

**Privacy.** `run.json` holds no hostname, username or absolute path: paths are relative to the run or server
directory, and the machine is identified by a fingerprint id and the profile's label. The harness scrubs every string
before writing and treats anything left as a fault, and `tests/unit/test_paths_scrub.py` checks it. Raw logs are the
server's own output and are not scrubbed, so check them before sharing.

**Schemas.** [`bedrock_bench/schemas/`](../bedrock_bench/schemas/) holds JSON Schema (draft 2020-12) for `run.json`
and `run.partial.json` (`bedrock-bench/run/1`), `metrics.json` (`bedrock-bench/metrics/1`) and each line of the bot's
output (`v = 1`). `run.schema.json` describes each section of `run.json`. `bbench validate <path>...` checks run
directories and files against them and checks raw files against the hashes in `run.json`. `summary.json`, the CSVs and
the TOML inputs have no schema. `bedrock_bench/config.py`
validates the inputs when it loads them. How a schema or the analysis version changes is in
[CONTRIBUTING.md](../.github/CONTRIBUTING.md#versioning).

## Publishing

`bbench report --publish` refuses a summary, and names each reason, when:

- a cell has fewer than 3 `ok` runs, unless every run of it failed;
- a run used a local artifact instead of a pinned release;
- a run is not official: a dirty harness checkout, a local artifact or a bot override;
- an exercised feature has `unknown` parity;
- a table that compares different servers ran with client-side generation on.

Every report also refuses mixed analysis versions, matrices, scenario hashes, machines and machine profiles. The code
cannot check these rules:

- Every run of the matrix is included, whatever its status. Invalid runs stay, with their reason.
- Numbers from a profiled, patched or otherwise non-standard run are not results
  ([CONTRIBUTING.md](../.github/CONTRIBUTING.md#profiling)).
- Parity rows and work tables are never cropped away from a cost table, and arms are never ranked across fidelity
  sections.
- The raw run bundles are published with their sha256, so anyone can run `bbench analyze` and get the same
  `metrics.json`.
- Nothing from Mojang is included: no server binaries or worlds.
- A publication is immutable. A correction is a new publication that names the one it replaces.

## Design decisions

### One bot for every server

The load generator decides what work a server is asked to do: how often clients send input, which chunks they request,
how they move. With a bot build or flag set per server, a difference between two servers could come from the bots.
All bot behaviour lives in one Go module, `bots/core`, and each protocol gets a thin frontend, because Go cannot link
two versions of gophertunnel into one module: `bots/loadbot` for the current protocol and `bots/legacy1001` for
PocketMine-MP and Allay. Every arm gets identical flags (R2). Adapters may add only allowlisted identity flags
(`-fake-xuid`), which the contract test enforces, and a server's needs are met with those flags or adapter-side config,
never by changing what the bot does for one server. Patches follow [bots/README.md](../bots/README.md#patches).

### No composite score

A single number has to weigh tick rate against CPU, memory, view-load time and features, and the weights encode a use
case. Servers also do different amounts of work for the same bots, and a score would rank skipped work as better
engineering. The report computes no score, never ranks arms of different fidelity against each other, sorts sections
by arm id, prints efficiency metrics only next to the parity rows they depend on, and says "better" only on complete
separation. Adding a server never reorders the others.

### Tick-thread CPU is not MSPT

The headroom metric is called tick-thread CPU per tick, because CPU time leaves out the time the tick thread is blocked
([metrics.md](metrics.md#tick-thread-cpu)). A server's own MSPT readout, if one is added, will be shown with its source
and never ranked across servers.

### Client-side generation off in every ranking scenario

With client-side generation on, a supporting client builds terrain from the seed and BDS sends only part of each view.
Only BDS and the servers that run inside it do this, so with it on, chunk work differs several-fold between server
families, and Endstone's numbers would mean different work in different tables. Every ranking scenario runs it off in
the server config and the bot, Endstone included, so every server builds the full view disc, 317 columns at view
distance 10. `spread100-csg` keeps it on only to reproduce Endstone's earlier figures on BDS-hosted servers, and
`report --publish` refuses it in a cross-server table.

## Threats to validity

- **One machine.** Absolute numbers do not transfer between machines. Ratios between arms on one machine transfer
  better, but not exactly: a server saturated on one machine may not be on another, and a server that is better on many
  cores may lose on few.
- **Bots are not players.** Every bot walks a circle, never changes height on its own, never builds, acknowledges
  nothing it does not have to, and misses every cached chunk. That is a worst case, applied equally to every server.
- **Movement corrections.** A bot claims flat walking over real terrain, so servers that validate movement send
  corrections a real client would not cause. They are counted as work.
- **Loopback.** All bots connect from 127.0.0.1 without encryption. Per-IP guards are raised and listed as overrides,
  and encryption cost is not measured.
- **Shared host.** The bots run on separate CPUs of the same machine but share memory bandwidth, the last-level cache
  and the power budget with the server.
- **Fresh worlds.** Terrain generation dominates the load phase of a fresh world. Mob-dense and redstone-dense
  workloads are not covered.
- **Virtual CPUs.** On a virtual machine or WSL2, pinning applies to virtual CPUs. Such runs are `vcpu` and carry a
  warning.
- **Declared parity.** Most parity cells are declared from public sources. Only features with an `observed_by` counter
  are checked against what the server did.
- **Variant parity.** Parity rows describe a server version, not a variant, so a variant is recorded with its server's
  row. The server's README lists what each variant changes.
- **Server defaults.** A server's defaults may be tuned for another workload. The harness changes them only for the
  reasons above and records every change.
