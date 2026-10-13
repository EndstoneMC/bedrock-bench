# Methodology

How bedrock-bench runs the benchmark and how to read what it produces. The workload is set by a config in
[`configs/`](../configs/), each server's pins are in `servers/<id>/server.toml` and its config changes in the templates
next to it, and metric formulas and gate thresholds are in [metrics.md](metrics.md). This page explains the method and
points at those files for the exact values.

## Rules

Code and error messages cite these rules by number.

| Rule | Statement | Enforcement |
|---|---|---|
| R1 | One workload config for every server in a session. Nothing in it changes per server. | `run.json` records the resolved config and its hash, and a report refuses runs with different hashes. |
| R2 | One bot source with the same flags for every server. Only `-fake-xuid` may be added, for servers that need it. PocketMine-MP and Allay get the protocol 1001 build of the same bot, flagged `bot_protocol_differs`. | Adapter contract test; report flag. |
| R3 | The same server CPU set, bot CPU set, priority and JVM heap for every server in a session. | One machine profile per session; a report refuses mixed machine profiles. |
| R4 | One measured run at a time, with continuous interference sampling. | Machine lock, idle gate, per-window foreign-load gate. |
| R5 | A fresh server copy and a fresh world for every run. Nothing is edited in place. | Run directory layout, install tree hash, `world_fresh` gate. |
| R6 | Every value the harness changes is recorded, and its reason is written next to it in the server's folder. | Config files are rendered from templates in `servers/<id>/` with a one-line reason per key; `run.json` keeps every key before and after; `config_readback` gate. |
| R7 | No interventions (nudges, re-teleports, commands other than the tick probe) inside measured windows. | `interventions_in_measured` gate. |
| R8 | TPS comes only from a counter that advances once per executed tick. World time and day time are rejected. | Adapters declare the probe monotonic; the probe must advance during idle. |
| R9 | Crashes and join failures are outcomes. They are never dropped or silently rerun. | Run status ([below](#statistics-and-run-status)). |
| R10 | Every number in a report is computed from a run's raw files by the same code for every server. | `bbench analyze` rebuilds `metrics.json` byte for byte; the report takes every number from `metrics.json` and refuses mixed analysis versions. |

## Workload

There is one workload: players spread out on a grid, each loading its own full view, with client-side generation off.
It is the worst common case for a survival server, and every extra workload would be one more place to pick a result
that suits one server.

[`configs/spread.toml`](../configs/spread.toml) holds every setting except the bot count, so it cannot run on its own.
The other configs set `base = "spread"` and change only what their name says:

| Config | Bots | Use |
|---|---|---|
| [`spread-100.toml`](../configs/spread-100.toml) | 100 | The reference. |
| [`spread-50.toml`](../configs/spread-50.toml) | 50 | Machines that cannot drive 100 bots. |
| [`spread-25.toml`](../configs/spread-25.toml) | 25 | Machines that cannot drive 50 bots. |
| [`smoke.toml`](../configs/smoke.toml) | 4 | `bbench smoke`: short phases, view distance 4, one run. It checks that a server works and is never a result. |

The loader merges the base under the config and refuses a config that leaves a key unset. Every run stores the
resolved values, the base chain and their hash in `run.json`. Results compare only within one config (R1), and a report
names its config and bot count at the top.

The values below are those in `spread.toml`.

- **Grid.** Bot `i` stands at `(1280 + (i % 11) * 640, 1280 + (i // 11) * 640)`, teleported to height 300. Cells are
  640 blocks apart, twice the width of a view at view distance 10, so no two players share a chunk or see each other's
  terrain. Spread players are the worst case for a server: each one needs its own terrain generated, loaded, ticked and
  sent. Clustered players share chunks and flatter the server.
- **Server settings.** View distance 10, simulation distance 4, seed 20261010, easy, survival, room for 200 players,
  autosave off with player data saved, client-side generation off. Each adapter writes them into its server's own
  config files ([below](#server-configuration)). BDS has no autosave switch, so BDS, Endstone and LeviLamina keep its
  incremental saver, which writes at most one changed chunk per tick.
- **World.** No world folder exists before launch (`world_fresh` checks it), and the server generates the world with
  its own generator during the run. Servers with their own generator build different terrain from the same seed, and
  some build only flat worlds. Each server's README says which.
- **Placement.** Once a bot reports `spawned` and the server prints its player-in-world line, the harness gives the bot
  effects so it cannot die from falls, fire, water or hunger, and teleports it to its cell. The server generates the
  area while the bot falls. `spreadplayers` is never used, because it puts players at unpredictable heights in
  ungenerated terrain.
- **Bots.** The same Go bot joins every server ([bots/README.md](../bots/README.md#flags)), and the bots dial 300 ms
  apart. Each logs in offline over RakNet from 127.0.0.1 with a fixed identity, walks a circle about 43 blocks across,
  reports every cached chunk as missing so the server always sends full chunks, and counts the distinct chunks in its
  current view. A decode error makes the run invalid, because the bot did not see everything the server sent.

## Run phases

```
bbench run CONFIG [TAG ...]
  runner:  take the machine lock, check the placement, fetch every server, then start one `bbench run-one`
           process per run, servers rotated across the repeats, with a cooldown between runs
  run-one:
    fetch      verified, read-only install from the cache
    idle gate  sample the machine and refuse to start under foreign load
    prepare    fresh server copy, rendered configs, no world folder
    launch     pinned and prioritised before the first server thread runs
    console    log reader, tick probe, samplers, death watch
    phases     boot -> idle -> join -> settle -> spread -> steady -> teardown
    results    run.json, raw series, metrics.json, status, exit code
  runner:  report.md, summary.json and the CSV files for the session
```

With no TAG, a session runs each server's default version from the `[benchmark]` table of its `server.toml`. It skips
a server, and prints why, when the server is marked `include = false`, does not run on this platform, or needs a bot
that is not available. Today that leaves out Chorus, whose player list gives the bots decode errors, and PocketMine-MP
and Allay until the protocol 1001 bot can be built. LeviLamina runs on Windows only. The report lists every skipped
server with its reason. A TAG that cannot run is an error.

Each run is its own process, so a crash or a hang cannot leak state into the next. `run.partial.json` is rewritten
after every phase, so an interrupted run keeps what it measured. A `spread-100` run takes 10 to 20 minutes, most of it
in settle.

| Phase | From | To | What happens |
|---|---|---|---|
| boot | launch | the server's ready line | The version banner must match the tag. At most 600 s. |
| idle | ready + 10 s | + 20 s | No bots. Idle baseline and thread census. The tick probe must advance. |
| join | first dial | `alljoined` | Bots dial 300 ms apart and are placed as they enter the world. At most 300 s. |
| settle | `alljoined` | `settled` | Views fill. Interventions are allowed here and only here. 300 s to 900 s. |
| spread | `settled` | | Effects sent again, positions read back from the server, spread checked. |
| steady | `settled` + 15 s | + 90 s | Only the tick probe is sent. |
| teardown | | | Bots get `quit`. The server gets its stop command and 180 s to exit, then its process tree is killed. Crash files are collected. |

| Window | Interval | Used for |
|---|---|---|
| `idle` | `[idle0, idle1]` | the probe advancing with nobody online |
| `load` | `[bots_start, settled]` | lowest TPS, view load time, chunks per second |
| `steady` | `[steady0, steady1]` | TPS, tick-thread CPU, server CPU, memory |

`load` and `steady` are the measured windows. The probe, interference and bot CPU gates apply to both.

**Settle.** Settle ends once 300 s have passed since `alljoined` and the bots' combined count of distinct chunks has
grown by at most 0.1 chunks per bot per second over the last 60 s. At 900 s it ends anyway, with the flag
`settle_capped`. The rule is checked every 5 s, on bot data at most 5 s old. If at `settled` fewer than 95% of the bots
hold half the median bot's in-view count, the run is flagged `views_incomplete`. A low chunk rate alone does not mean
loading is done, because a saturated server also delivers slowly. That is why there is a 300 s floor, and why the
length of settle is never a speed metric.

**Interventions.** Both are bounded, counted per bot, recorded in `run.json` and never happen after `settled` (R7).

- Re-teleport: a bot whose teleport the server has not confirmed 20 s after placement gets it again, up to 3 times.
- Nudge: after 60 s of settle, a bot still above y 290 with fewer than 30 distinct chunks is teleported over its cell
  again, 16 blocks aside on alternate tries, up to 2 times.

**Spread.** After settle the harness reads every player's position from the server (`querytarget @a` on BDS-hosted
servers, a helper command on the others) and maps each to its nearest cell. An adapter without a positions command
would use the positions the bots report, labelled as such. The spread is valid when every joined bot has its own cell.
The worst offset from a cell is recorded, with a warning above 200 blocks. Unknown positions make the run invalid. They
are never read as zero players.

**Tick probe.** Every 5 s the harness sends the server's tick command through a priority lane of the console writer,
ahead of any queued command, and stamps each reply with its monotonic clock. TPS over a window is the counter
difference divided by the reply-time difference at the window's edges (R8). The counter must advance during idle, which
catches servers that only tick worlds with players in them. A busy server answers the probe late, since most servers
run console commands on the tick thread, so the limits only catch a stuck probe: a gap of more than 60 s between
replies, or a p99 reply time above 30 s in steady, makes the run invalid. Rates over spans shorter than a window are
dominated by reply jitter and are never reported alone.

| Sampler | Rate | Source |
|---|---|---|
| threads | 1 Hz | CPU time and name of every server thread |
| tick thread | 10 Hz | CPU time of the thread the adapter's tick-thread rule picks |
| process | 1 Hz | process and process-tree CPU, memory, threads, handles |
| machine | 1 Hz | per-CPU busy time; foreign load is machine busy time minus the server, bot and harness trees |
| foreign top | every 10 s | the busiest foreign processes |

## Metrics

[metrics.md](metrics.md) is generated from `bedrock_bench/metrics/registry.py` and lists every metric with its unit,
window, inputs and formula. Metrics are pure functions of a run's raw files: `bbench analyze` recomputes
`metrics.json` byte for byte under the same analysis version (R10). The report shows these for each server, in this
order:

| Column | Metric | What it is |
|---|---|---|
| Runs ok | | `ok` runs out of all runs |
| TPS | `tps_window_steady` | ticks per second over steady, from the probe counter |
| Tick-thread CPU per tick (ms) | `tick_cpu_ms_per_tick_steady` | CPU time of the tick thread per executed tick, out of a 50 ms budget. Not MSPT ([below](#tick-thread-cpu-is-not-mspt)). |
| Server CPU (cores) | `process_cores_steady` | CPU seconds per second of the server process, all threads included |
| Memory (MiB) | `private_mib_steady` | private memory of the server process, mean over steady |
| Lowest TPS while views load | `tps_load_low5` | lowest 5 s tick rate between the first dial and `settled` |
| View load time (s): median, p90 and slowest player | `view_t90_own_s` | per player, seconds from its teleport until it holds 90% of the chunks in its view at `settled` |
| Chunks per second while views load | `chunks_per_s_load` | the chunks in every player's view at `settled`, divided by the time from the first dial to `settled` |
| Chunks in view per player | `chunks_per_bot` | chunk columns in a player's view at `settled`, median over players. A view of radius 10 holds 317. |

View load time and chunks per second are the chunk load measures. View load time is relative to each player's own
final view, so a server that sends fewer chunks finishes sooner. Read it next to chunks in view per player.

Server CPU covers the server process only. Endstone's Python launcher, which waits next to the server, is sampled
separately and not included. LeviLamina's CrashLogger is a separate process and is not measured.

Every metric has an availability: `measured`, `not_applicable` (the server has no such quantity, such as tick-thread
CPU on Dragonfly, which has no single tick thread) or `unavailable` (it should exist but could not be computed; a reason
says why). `metrics.json` also holds the inputs of the validity gates: probe gaps and latency, bot and foreign CPU,
decode errors, joins and drops.

## Server configuration

Every server runs its pinned release as its project ships it, with the fewest changes that let the benchmark drive it.
The pins, with their sha256, are in the `[artifacts]` table of `servers/<id>/server.toml`.

Each config file the harness writes is rendered from a template in the server's folder, named like the real file and in
its format, for example [`servers/bds/server.properties`](../servers/bds/server.properties). A template lists only the
keys the harness sets, each with a one-line comment saying why. JSON has no comments, so LeviLamina's reasons are in
its README. The adapter renders the template with Jinja from the config's values and the run's own (port, server CPU
count) and merges the result onto the file in the server copy: properties keys are set or added, YAML, TOML and JSON
are deep-merged, and a file that does not exist yet is written as rendered. `run.json` records every key's value
before and after, and the sha256 of every written file. `config/written/` and `config/after/` keep the files, and a
value that changed by the end of the run fails `config_readback`.

Endstone and LeviLamina keep their own copy of BDS's `server.properties` template, and a test checks that the copies
are identical, so the servers that host BDS run the same settings as vanilla BDS.

The changes come in three kinds:

- **Driving the server.** Offline login, ports, transport, console, a helper plugin that adds a tick counter or a
  positions command where a server has none, raised per-IP limits, and telemetry, crash upload, usage statistics and
  update checks off where a switch exists.
- **The same setup everywhere.** The config's gameplay settings. One JVM heap for every JVM server (`-Xms` equal to
  `-Xmx`, from the machine profile). Worker pools sized to the server's CPU set where the server would otherwise count
  every CPU of the machine. Automatic tick-rate reduction off on Nukkit, Nukkit-MOT and PowerNukkitX, because it ticks
  a slow level less often while the tick counter keeps advancing.
- **Finishing the join.** PocketMine-MP and Altay ship a 1024 MB main-thread memory limit that stops the server during
  the 100-player join. The limit is removed.

Nothing else changes. No server gets tuning its project does not ship, an optimisation plugin or a tuned build. A
server whose tick stalls is not helped: Nukkit's and Nukkit-MOT's watchdogs stay on, and a run they stop is a
`server_failed` result.

- **Loopback.** All bots connect from 127.0.0.1, which trips per-IP flood guards real players never hit. The templates
  raise them. LeviLamina's guard stays at its default, because the bots stay within it, and Nukkit-MOT's already
  exempts the machine's own addresses. A server that drops bots is flagged `dropped`.
- **Leftovers.** Where a server has no switch for an upload or update check, its README says so: Endstone uploads a
  crash report when it crashes, and Nukkit-MOT asks GitHub for its latest commit at start.
- **BDS build.** A server that hosts BDS is compared with vanilla BDS of the same build. Endstone 0.11.14 hosts the
  default BDS build, 1.26.52.3. LeviLamina 26.51.6 hosts 1.26.51.1, pinned with LeviLamina, so it is flagged
  `different_bds_build`.
- **Confounders are disclosed, never subtracted.** The allocator (LeviLamina links mimalloc, and Endstone 0.11.14
  preloads it on Linux) and the JVM heap are recorded in `run.json`, and their cost stays in the numbers.

## Placement and interference

The machine profile is a TOML file passed with `--machine`. Without one, built-in defaults apply. An example is in
[get_started.md](get_started.md#machine-profile). It names three CPU sets and a priority:

- **Server CPUs.** `auto` gives the server every CPU of the fastest class on a hybrid CPU. Otherwise the bots get whole
  physical cores, slowest first, until they have a quarter of the cores, and the server gets the rest. SMT siblings
  stay together. An explicit list such as `0-15` is used as is.
- **Bot CPUs.** `auto` takes the rest. Fewer than 4 logical CPUs fails `bbench doctor`, because a starved bot shows up
  as server slowness.
- **Harness CPUs.** The bot set, unless the profile says otherwise.
- **Priority.** `normal` unless the profile says otherwise. Every server in a session gets the same priority, and it is
  recorded.

On Windows the server starts suspended, gets its affinity and priority, and only then resumes, so thread pools sized at
startup see the restricted set. On Linux affinity and priority are set between `fork` and `exec`. The affinity of the
server and every one of its threads is checked at `ready`, `steady0` and `steady1` (`placement_match`). Placement is
recorded as `physical`, or as `vcpu` on a virtual machine or WSL2, where pinning applies to virtual CPUs the host may
move between cores.

A session runs on one machine and one platform. The maintainers' runs use Windows. A report refuses runs from
different machines (the fingerprint includes the operating system family) or different machine profiles (R3).

Interference is handled in three layers (R4):

1. **Machine lock.** One measured run per machine at a time. The lock is an operating-system file lock, released when
   the process dies.
2. **Idle gate.** Before each run the harness samples the machine for 15 s. Under foreign load the run does not start
   and the session stops.
3. **Window gate.** Foreign load is sampled every second. A run whose measured windows exceed the profile's `[gates]`
   limits (mean, 10 s peak, and load on the server's CPUs) is invalid. The busiest foreign processes are recorded so
   the cause can be found.

The bots share the host with the server, on separate CPUs. A run whose bot CPUs are more than 85% busy is invalid
(`bot_saturated`), because a starved bot under-reports what the server delivers. How to prepare a machine is in
[get_started.md](get_started.md#preparing-the-machine).

## Statistics and run status

- **Session.** One `bbench run` is a session. Each server runs 3 times, and each repeat starts one server later in the
  order than the one before, so no server always runs first or last. A 60 s cooldown and the idle gate separate runs.
  These are the `[run]` values of the config.
- **Aggregation.** The report shows the median, minimum and maximum of the `ok` runs, with `ok` runs out of all runs.
  `server_failed` and `invalid` runs count in the total, never in a median.
- **No outlier removal.** A run is either invalid by a gate or kept.
- **Comparability.** A report never mixes configs, machines, machine profiles or analysis versions.

**Status.** Each gate in [metrics.md](metrics.md#validity-gates) fails a run as `invalid` or `server_failed`, or adds a
warning. The status is `invalid` if any invalid gate fails, else `server_failed` if an outcome gate fails, else `ok`.
`invalid` means the harness or the machine failed, and the run is rerun once. `server_failed` is a result about the
server and is never rerun (R9). Gates on a window the run never reached are skipped, so a server that dies early is
`server_failed`, not invalid. Exit codes come from the recorded statuses, so a session in which a server died never
exits 0.

Flags never change the status but always appear next to the numbers: `settle_capped`, `views_incomplete`,
`bot_protocol_differs`, `different_bds_build`, `unpublished_artifact` and `dropped`.

When the server dies or exits nonzero, the harness copies the last 200 log lines, and every file modified since the run
started under the adapter's crash paths, into `crash/`.

## Reading a report

`report.md` is laid out like the results section of a paper:

- the config and its bot count;
- an Environment table: CPU model, logical CPUs given to the server and to the bots, RAM, OS and kernel, Python,
  harness version, bot build and each server's tag;
- one Results table, one row per server, with the columns [above](#metrics). Each cell is the median with [min, max]
  over the `ok` runs, and units are in the header;
- one line for each run that failed or was invalid, each flag, each skipped server and each metric a run could not
  provide.

`bbench report` also writes `summary.csv`, `tps.csv` and `view_load.csv` next to it. `scripts/plot_results.py` draws
from them TPS over time, one chart per metric and the spread of view load time, into `results/`.

1. **20 TPS is a ceiling, not a score.** No server runs faster than 20 TPS, so two servers that both hold it can be far
   apart. Compare them on headroom: tick-thread CPU per tick against the 50 ms budget of a tick. Below 20 TPS the tick
   thread is saturated and TPS itself is the measure. Go servers have no single tick thread, so compare server CPU.
2. **The same workload is not the same work.** A server with a flat world, or without player physics, does less for
   the same bots. Each server's README lists what it does differently under Caveats. View load time is relative to
   each player's own view, so read it with chunks in view per player. Chunks per second divides by the whole load
   window, which lasts at least 300 s after the last join, so it separates servers only when loading takes longer than
   that.
3. **Ranges, not points.** With 3 runs each, call one server better on a metric only when every run of it beats every
   run of the other. The chance of that with no real difference is 1 in 20. Overlapping ranges mean "not separated",
   not "equal". Run-to-run noise on a desktop is large enough that a small gap between two medians usually means
   nothing.
4. **Failures are results.** A crash, a watchdog stop or a server that cannot take every player is `server_failed`,
   shown as `k/n` runs ok. `invalid` runs say something about the harness or the machine, not the server.
5. **Memory differs by runtime.** A JVM server's memory is mostly its fixed heap, so memory compares well only between
   servers on the same runtime.

## Results files

A session writes one directory per run and the aggregate next to them:

```
runs/<session id>/
  <run id>/
    run.json             provenance, resolved config, config changes, marks, interventions, spread, status
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
  session.json           the servers the session skipped, with the reason
  summary.json           the report's numbers per server, aggregated over the repeats
  summary.csv            the same per server: runs ok and total, median, min and max of each metric
  tps.csv                every probe sample of every ok run
  view_load.csv          view load time of every player in every ok run
  report.md              the rendered report
```

The session id is `<UTC yyyymmddThhmmssZ>-<4 hex>`. The run id is
`<UTC yyyymmddThhmmssZ>-<server>@<version>-r<repeat>-<4 hex>`, for example
`20261012T135800Z-endstone@0.11.14-r1-3fa2`. `run.json` also records the session id, the repeat and the run's place in
the order. Run directories are never overwritten or deleted. `bbench report runs/<session id>` renders the report again
from them.

**Raw formats.** `server.log.gz` has one line per console line, `<mono_ns>\t<epoch_ms>\t<line>`, with ANSI escapes
stripped. `commands.jsonl.gz` has one object per console write with its enqueue and send times, lane and origin.
`bots.jsonl.gz` has the bot's events ([bots/README.md](../bots/README.md#events)), each with `rx_ns`, the time the
harness read it. Every CSV starts with a header row, and `bedrock_bench/sampling/series.py` lists the columns. Memory is
in MiB (2^20 bytes).

**Time.** Every harness timestamp is monotonic nanoseconds (`*_ns`) from one clock, taken when the harness reads the
data. `run.json` stores one origin pair taken together at the start, `time.origin_mono_ns` and `time.origin_epoch_ms`,
which maps epoch-stamped sources such as bot events onto the monotonic timeline. `time.drift_ms` records how far the
two clocks drifted apart during the run. Phase marks are seconds since the origin.

**Privacy.** `run.json` holds no hostname, username or absolute path. Paths are relative to the run or server
directory, and the machine is identified by a fingerprint id and the profile's label. The harness scrubs every string
before writing and treats anything left as a fault, which `tests/unit/test_paths_scrub.py` checks. Raw logs are the
server's own output and are not scrubbed, so check them before sharing.

**Schemas.** [`bedrock_bench/schemas/`](../bedrock_bench/schemas/) holds JSON Schema (draft 2020-12) for `run.json` and
`run.partial.json` (`bedrock-bench/run/1`), `metrics.json` (`bedrock-bench/metrics/1`) and each line of the bot's output
(`v = 1`). `run.schema.json` describes each section of `run.json`. `bbench validate <path>...` checks run directories
against them and checks raw files against the hashes in `run.json`. `summary.json`, the CSV files and the TOML inputs
have no schema. `bedrock_bench/config.py` validates the TOML inputs when it loads them. How a schema or the analysis
version changes is in [CONTRIBUTING.md](../.github/CONTRIBUTING.md#versioning).

## Publishing

`bbench report --publish` refuses a session, and names each reason, when:

- a server has fewer than 3 `ok` runs, unless every run of it failed;
- a run used a local build instead of a pinned release (`unpublished_artifact`);
- a run is not official: it ran with uncommitted changes to tracked files under `bedrock_bench/`, `bots/`, `configs/`
  or `servers/`, or with a bot override.

Every report also refuses mixed configs, machines, machine profiles and analysis versions. The code cannot check these
rules:

- Every run of the session is included, whatever its status. Invalid runs stay, with their reason.
- Numbers from a profiled, patched or otherwise non-standard run are not results
  ([CONTRIBUTING.md](../.github/CONTRIBUTING.md#profiling)).
- The notes under the Results table are never cropped away from it.
- The raw run directories are published with their sha256, so anyone can run `bbench analyze` and get the same
  `metrics.json`.
- Nothing from Mojang is included: no server binaries and no worlds.
- A publication is immutable. A correction is a new publication that names the one it replaces.

## Design decisions

### One bot for every server

The load generator decides what work a server is asked to do: how often clients send input, which chunks they request,
how they move. With a bot build or flag set per server, a difference between two servers could come from the bots. All
bot behaviour lives in one Go module, `bots/core`, and each protocol gets a thin frontend, because Go cannot link two
versions of gophertunnel into one module: `bots/loadbot` for the current protocol and `bots/legacy1001` for
PocketMine-MP and Allay. Every server gets the same flags (R2). Adapters may add only `-fake-xuid`, which the contract
test enforces, and a server's needs are met with that flag or with config, never by changing what the bot does for one
server. Patches follow [bots/README.md](../bots/README.md#patches).

### No composite score

A single number has to weigh tick rate against CPU, memory and chunk load time, and the weights encode a use case.
Servers also do different amounts of work for the same bots, and a score would rank skipped work as better
engineering. The report computes no score and lists servers in a fixed order, so adding a server never reorders the
others.

### Tick-thread CPU is not MSPT

The headroom metric is called tick-thread CPU per tick, because CPU time leaves out the time the tick thread is blocked
([metrics.md](metrics.md#tick-thread-cpu)). A server's own MSPT readout, if one is added, will be shown with its source
and never compared across servers.

### Client-side generation off

With client-side generation on, a supporting client builds terrain from the seed and BDS sends only part of each view.
Only BDS and the servers that run inside it do this, so with it on, chunk work differs several times over between
server families, and the same server's numbers would mean different work in different setups. It is off in the server
config and in the bots' login for every server, Endstone included, so every server builds and sends the full view.

## Threats to validity

- **One machine.** Absolute numbers do not transfer between machines. Ratios between servers on one machine transfer
  better, but not exactly: a server saturated on one machine may not be on another, and a server that is better on many
  cores may lose on few.
- **One platform.** The maintainers measure on Windows. Allocators, schedulers and server builds differ on Linux, so
  Linux results can order servers differently.
- **Fewer bots.** `spread-50` and `spread-25` load a server less. A server that holds 20 TPS at 25 bots may not at 100,
  so their results do not stand in for `spread-100`.
- **One workload.** Mob-dense, redstone-dense, building and combat workloads are not covered. Terrain generation
  dominates the load phase of a fresh world.
- **Bots are not players.** Every bot walks a circle, never changes height on its own, never builds, acknowledges
  nothing it does not have to, and misses every cached chunk. That is a worst case, applied equally to every server.
- **Movement corrections.** A bot claims flat walking over real terrain, so servers that check movement send
  corrections a real client would not cause. Their cost stays in the numbers.
- **Loopback.** All bots connect from 127.0.0.1 without encryption. Per-IP guards are raised in the templates, and
  encryption cost is not measured.
- **Shared host.** The bots run on separate CPUs of the same machine but share memory bandwidth, the last-level cache
  and the power budget with the server.
- **Virtual CPUs.** On a virtual machine or WSL2, pinning applies to virtual CPUs. Such runs are `vcpu` and carry a
  note.
- **Server defaults.** A server's defaults may be tuned for another workload. The harness changes them only for the
  reasons above and records every change.

## Conflict of interest

bedrock-bench is written and run by EndstoneMC, the maintainers of Endstone, one of the servers it measures. To keep
that from shaping the results, every server gets the same workload config, bot and metric code, every config change
sits in a template in the server's folder with its reason, and the raw files of a published session let anyone
recompute its numbers. If a server is set up wrong or described wrongly, open an issue or a pull request with the fix.
