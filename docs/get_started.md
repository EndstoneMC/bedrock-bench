# Getting started

This page takes a fresh clone to a finished benchmark session: what the machine needs, how to prepare it, how to
describe it to the harness, and how to read the output. Run every command from the root of the checkout.

## Requirements

- Windows 10 or 11 x64, or Linux x86_64. Measured runs need bare metal. Other systems can run `bbench analyze`,
  `report` and `validate` on existing results, but cannot run servers.
- [uv](https://docs.astral.sh/uv/). It installs the Python version in `.python-version` when it is missing. The
  harness needs Python 3.11 or newer, and its only runtime dependencies are `psutil` and `jinja2`.
- Go 1.25 or newer to build the load bot. Dragonfly needs Go 1.26 or newer, because fetch builds its helper from
  source.
- Rust 1.95.0 through [rustup](https://rustup.rs/) and a C compiler (the MSVC build tools on Windows), only to run
  Chorus, which fetch also builds from source.
- At least 4 logical CPUs for the bots, on top of the CPUs the server gets.
- Network access the first time each server is fetched and the bot is built.

Nothing else needs installing. Nukkit, Nukkit-MOT, PowerNukkitX and Allay run on the JDKs, and PocketMine-MP and Altay
on the PHP build, pinned in [`servers/runtimes.toml`](../servers/runtimes.toml), which fetch downloads. Endstone gets
its own virtual environment, built with uv. LeviLamina runs on Windows only.
[README.md#supported-servers](../README.md#supported-servers) lists every server and its pinned versions.

## Installation

```shell
git clone https://github.com/EndstoneMC/bedrock-bench.git
cd bedrock-bench
uv sync
uv run bbench --version
uv run bbench bots build
```

- `uv sync` creates `.venv` with the harness and the development tools.
- `bots build` builds `loadbot`, the bot for every server except PocketMine-MP and Allay, into
  `bots/bin/<os>-<arch>/`. Those two need `legacy1001`, the protocol 1001 bot, which cannot be built until its
  gophertunnel fork tag is published ([bots/README.md#protocol-1001](../bots/README.md#protocol-1001)). On a released
  version, `uv run bbench bots fetch` downloads the release binary instead and checks it against the release's
  `SHA256SUMS`.
- Downloads and installs go to the cache: `%LOCALAPPDATA%\bedrock-bench` on Windows, `~/.cache/bedrock-bench` on Linux
  (or `$XDG_CACHE_HOME/bedrock-bench`), or `$BBENCH_CACHE` when set. Results go to `runs/` in the working directory,
  which git ignores.

## Preparing the machine

```shell
uv run bbench doctor
```

`doctor` prints the CPU topology, the CPUs it would give the server, the bots and the harness, the power setting,
whether the machine lock is free, the foreign load over 15 s, the server's UDP ports and the Go version. Foreign load is
the CPU used by every process other than the server, the bots and the harness. `doctor` exits with 3 when a check
fails, and `--json` prints the same report as JSON.

Before a measured session:

- **Quiet machine.** Close browsers, chat clients, game launchers and any Minecraft client. Stop other benchmarks and
  builds. The harness measures foreign load before every run and refuses to start when it is too high. A run with too
  much foreign load in a measured window is `invalid`.
- **Power.** On Windows, use the High performance or Ultimate Performance plan and stay on mains power. On Linux, use
  the `performance` governor. Turbo can stay on, but must stay the same for the whole session.
- **Thermals.** A laptop or a small case may throttle during long runs. If results drift from one repeat to the next,
  watch the CPU temperature and clock speed during a run.
- **CPUs.** The bots need at least 4 logical CPUs outside the server's set. A run whose bot CPUs are more than 85% busy
  is `invalid`. Never put the server on efficiency cores, and never give the bots SMT siblings of server CPUs. `doctor`
  warns about both.
- **Microsoft Defender.** Real-time scanning of the cache and `runs/` costs CPU during runs. Add exclusions for both if
  your policy allows.
- **Path length on Windows.** Server files must stay under the 259-character path limit, and fetch refuses an install
  that would not fit. If your user profile path is long, set `BBENCH_CACHE` to a short directory such as `C:\bb`.
- **Loopback exemption on Windows.** Endstone's launcher checks before every start that the Minecraft app may connect
  to loopback, and asks for elevation if not, which stalls an unattended session. Add the exemption once from an
  elevated prompt, as [its server page](../servers/endstone/README.md#setup) shows.
- **One benchmark at a time.** Every run holds the machine lock, `machine.lock` in the cache. Benchmarks that share a
  machine must share the lock: keep the default cache, or point `BBENCH_LOCK` at one common file.

Virtual machines, WSL2 and cloud instances can only pin virtual CPUs. `doctor` warns, runs are recorded with placement
`vcpu`, and the report notes it. Such runs are fine for development but not comparable with bare-metal runs. On shared
cloud hosts, noisy neighbours show up as run-to-run variance that the foreign load check cannot see.

## Machine profile

A machine profile is a TOML file that says which CPUs the server, the bots and the harness get, and the foreign load
limits every run must stay within. Without `--machine`, the defaults below apply and the CPU sets are chosen
automatically:

- On a hybrid CPU, the server gets every core of the fastest class, if that leaves the bots at least 4 logical CPUs.
- Otherwise the bots get whole cores from the slowest end until they have a quarter of the cores and at least 4
  logical CPUs, but never more than half. The server gets the rest.
- SMT siblings always stay together.

To set the CPUs yourself, copy this example to a file such as `local/my-machine.toml` (git ignores `local/`) and edit
it. Every key is optional.

```toml
# Intel Core i7-14700F: 8 performance cores with SMT (CPUs 0-15), 12 efficiency cores (CPUs 16-27).

[placement]
server_cpus = "0-15"   # default "auto"; or a list such as "0-7,16"
bot_cpus = "16-27"     # default "auto"; next to an explicit server_cpus, every other CPU; at least 4
harness_cpus = "bot"   # default "bot": share the bot CPUs; or a list away from the server
priority = "normal"    # normal | above_normal; above_normal on Linux needs root or CAP_SYS_NICE

[runtime]
port = 19672           # the server's IPv4 UDP port; IPv6 uses the next one
jvm_heap = "8G"        # -Xms and -Xmx for every JVM server

[gates]
idle_check_s = 15.0                   # seconds of foreign load measured before each run
foreign_cores_mean = 0.25             # mean foreign load in cores, before the run and in every measured window
foreign_cores_peak_10s = 1.5          # highest 10 s mean of foreign load
foreign_server_set_cores_mean = 0.25  # mean foreign load on the server's CPUs
bot_cpu_frac_max = 0.85               # busiest the bot CPUs may be in a measured window
```

Check what it resolves to:

```shell
uv run bbench doctor --machine local/my-machine.toml
```

The machine label is the file name without `.toml`, unless the file sets `label`. Every run records the label, a hash
of the profile and the machine it ran on. A report never mixes machine profiles or machines: `bbench report` refuses a
directory that does. The label and the port are left out of the hash.
[`.github/ci-machine.toml`](../.github/ci-machine.toml) loosens the limits for CI and is never used to measure.
[docs/methodology.md#placement-and-interference](methodology.md#placement-and-interference) explains how placement is
applied and checked.

## First run

This checks the whole chain with vanilla BDS and 4 bots:

```shell
uv run bbench doctor
uv run bbench fetch bds@1.26.52.3 --accept-eula
uv run bbench smoke configs/smoke.toml bds@1.26.52.3
```

- `fetch` downloads the pinned BDS archive from Mojang, checks it against the sha256 in
  [`servers/bds/server.toml`](../servers/bds/server.toml) and unpacks it into the cache. `--accept-eula` (or
  `BBENCH_ACCEPT_MINECRAFT_EULA=1`) accepts the [Minecraft End User License Agreement](https://www.minecraft.net/eula),
  and for LeviLamina also the LeviMC EULA that covers PreLoader and PeEditor. Nothing from Mojang is stored in this
  repository. `--artifact-dir DIR` takes files with their pinned names from DIR instead of downloading them, for
  example a build the upstream no longer serves.
- [`configs/smoke.toml`](../configs/smoke.toml) is the spread setup with 4 bots, view distance 4, short phases and one
  run. `smoke` passes when the run is `ok` and the server answers every command the harness sends without an error.
  It prints `smoke bds@1.26.52.3: passed` and writes the run under `runs/<session-id>/`. It takes a few minutes.

Check any other server the same way, with `uv run bbench smoke configs/smoke.toml <server>@<version>`.

## Running the benchmark

Every command that runs servers takes a workload config as its first argument. The configs in
[`configs/`](../configs/):

| Config | Bots | For |
|---|---|---|
| [`spread-100.toml`](../configs/spread-100.toml) | 100 | the reference benchmark |
| [`spread-50.toml`](../configs/spread-50.toml) | 50 | machines that cannot drive 100 bots |
| [`spread-25.toml`](../configs/spread-25.toml) | 25 | machines that cannot drive 50 bots |
| [`smoke.toml`](../configs/smoke.toml) | 4 | checking that a server runs end to end |

Each sets `base = "spread"` and takes everything else from [`spread.toml`](../configs/spread.toml): the grid the
players spread over, view and simulation distance, client-side generation off, the seed, the phases and the session
settings. `spread.toml` sets no bot count, so it cannot run on its own. Results are comparable only within one config,
and the report names its config and bot count at the top.

Use the largest config your machine can drive. The bots need at least 4 logical CPUs of their own, and a run whose bot
CPUs are more than 85% busy is `invalid` with the reason `bot_saturated`. Try one server first, and step down if its
runs come back `bot_saturated`. For another bot count, write a config that inherits from `spread`, for example
`local/spread-75.toml`:

```toml
base = "spread"

[bots]
n = 75
```

`base` always names a file in `configs/`. Changing a tracked file under `bedrock_bench/`, `bots/`, `configs/` or
`servers/` marks every later run `dirty_harness`: the report notes it, and `bbench report --publish` refuses those runs.

A full session:

```shell
uv run bbench fetch --accept-eula
uv run bbench run configs/spread-100.toml --machine local/my-machine.toml --dry-run
uv run bbench run configs/spread-100.toml --machine local/my-machine.toml
```

- With no tag, `fetch` and `run` take each server's default version from the `[benchmark]` table of its
  `servers/<id>/server.toml`. `run` has no `--accept-eula`, so fetch first, or set `BBENCH_ACCEPT_MINECRAFT_EULA=1`.
- They skip a server, and print why, when it is marked `include = false` (Chorus, until its player list decodes), when
  it does not run on this OS (LeviLamina on Linux), or when its bot is missing (PocketMine-MP and Allay, until the
  protocol 1001 bot can be built). The report lists the skipped servers. A tag given on the command line that cannot
  run is an error.
- One `bbench run` is one session. It holds the machine lock throughout, fetches anything still missing, then runs
  every server 3 times in rotated order with 60 s between runs, as the `[run]` table of the config says. The runs share
  a session id and land in `runs/<session-id>/`, and the session writes its report there at the end.
- `--dry-run` prints the run order and exits. `--out DIR` writes the session under DIR instead of `runs/`.
- An `invalid` run is rerun once. `server_failed` is a result and is never rerun. When a run refuses to start, for
  example because the machine is not idle, the session stops and reports the runs it has.
- A run takes 10 to 20 minutes, most of it the settle phase, which waits for chunk delivery to slow down (at least
  5 minutes, at most 15). Each server adds about half an hour to an hour to a session, not counting downloads.

To run some servers only, name their tags:

```shell
uv run bbench run configs/spread-100.toml bds@1.26.52.3 endstone@0.11.14 --machine local/my-machine.toml
```

## Reading the output

```
runs/<session-id>/
  <run-id>/        one run: run.json, metrics.json, the raw logs and series, config/, crash/
  session.json     the servers the session skipped
  summary.json     the core metrics over the repeats; report.md is rendered from it
  report.md        the results
  summary.csv      one row per server: runs ok and total, each core metric as median, min and max
  tps.csv          one row per TPS sample: run, server, version, seconds since all bots joined, TPS
  view_load.csv    one row per player per run: run, server, version, player, view load time in seconds
```

- A session id is `<UTC start time>-<4 hex>`, and a run id is `<UTC start time>-<server>@<version>-r<repeat>-<4 hex>`.
  Run directories are never overwritten.
- `run.json` records what ran: the downloads and their hashes, every config file the harness wrote with each key's
  value before and after, the config and its hash, the machine, the CPU placement, the foreign load and the run's
  status. `metrics.json` holds every metric computed from the raw files.
  [docs/methodology.md#results-files](methodology.md#results-files) has the full layout.
- `report.md` starts with the config and its bot count, then an Environment table (CPU, the CPUs given to the server
  and to the bots, RAM, OS, Python, harness version, bot build and each server's tag), one Results table with a row per
  server, and a one-line note for every run that was not `ok`, every flag and every skipped server. LeviLamina 26.51.6
  hosts BDS 1.26.51.1, not the `bds@1.26.52.3` baseline, so its row gets a `different_bds_build` note.
- The Results table has runs ok out of total, then one column per core metric: TPS, tick-thread CPU per tick, server
  CPU in cores, memory, the lowest TPS while views load, view load time for the median, p90 and slowest player, chunks
  per second while views load, and chunks in view per player. Each cell is the median over the `ok` runs with the
  lowest and highest run in brackets, `-` when nothing was measured and `n/a` when the metric does not apply. View load
  time runs from a player's teleport until it holds 90% of its view. [docs/metrics.md](metrics.md) defines every
  metric.

Every run ends with one status. `run.json` lists the reasons in `status_reasons`.

| Status | Meaning |
|---|---|
| `ok` | a result |
| `server_failed` | the server crashed, stopped or could not take every player; a result, counted in runs ok |
| `invalid` | the machine or the harness failed, for example foreign load, busy bot CPUs or a lost probe; not a result |

`bbench` exits with one of these codes. A session exits with the worst code of its runs.

| Code | Meaning |
|---|---|
| `0` | every run ok |
| `2` | usage or config error, or a report that `--publish` refuses |
| `3` | an environment check refused to start |
| `4` | a server failed |
| `5` | a run was invalid |
| `130` | interrupted |

Read [docs/methodology.md#reading-a-report](methodology.md#reading-a-report) before comparing servers. In short:

- 20 TPS is a ceiling. Two servers at 20 TPS differ in headroom, which tick-thread CPU per tick shows.
- View load time counts until a player holds 90% of the chunks it ends up with, so a server that sends fewer chunks
  finishes sooner. Read it next to chunks in view per player.
- Read the ranges, not only the medians.

To draw the figures:

```shell
uv run scripts/plot_results.py runs/<session-id>
```

The script reads the CSV files and writes PNG and SVG files into `results/`, or into `--out DIR`: TPS over time for
each server's median run, one chart per core metric, and the spread of view load time per server. uv installs its
plotting dependency on first use.

To recompute or check results:

```shell
uv run bbench analyze runs/<session-id>/<run-id>
uv run bbench validate runs/<session-id>
uv run bbench report runs/<session-id>
```

`analyze` recomputes `metrics.json` and the status from the raw files. `validate` checks every run against the schemas
in `bedrock_bench/schemas/` and every raw file against its hash in `run.json`. `report` rebuilds `report.md`, the
summary and the CSV files from the run directories. `report --publish` also checks the publication rules in
[docs/methodology.md#publishing](methodology.md#publishing).

To send results or report a problem, see
[.github/CONTRIBUTING.md#reporting-a-problem](../.github/CONTRIBUTING.md#reporting-a-problem).
