# Getting started

This page goes from a fresh clone to a finished experiment: what the machine needs, how to prepare it, how to describe
it to the harness, and where the results land. Run every command from the checkout.

## Requirements

- Windows 10/11 x64 or Linux x86_64. Measured runs need bare metal. macOS can run `bbench analyze`, `report` and
  `validate` on existing run directories, but cannot run servers.
- [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer. The only runtime dependency is `psutil`.
- Go 1.25 or newer to build the load bot. Dragonfly needs Go 1.26 or newer, because fetch builds its helper from
  source.
- Rust 1.95.0 through [rustup](https://rustup.rs/) and a C compiler (the MSVC build tools on Windows), only for
  Chorus, which fetch also builds from source.
- At least 4 logical CPUs for the bots, on top of the CPUs the server gets.
- Network access the first time each server is fetched and the bot is built.

Nothing else needs installing. Nukkit, Nukkit-MOT, PowerNukkitX, Allay, PocketMine-MP and Altay run on the JDK and
PHP builds pinned in [`configs/servers/runtimes.toml`](../configs/servers/runtimes.toml), which fetch downloads.
Endstone gets its own virtual environment, built with uv. LeviLamina runs on Windows only.
[README.md#supported-servers](../README.md#supported-servers) lists every server and its pinned version.

## Installation

```shell
git clone https://github.com/EndstoneMC/bedrock-bench.git
cd bedrock-bench
uv sync
uv run bbench --version
```

`uv sync` creates `.venv` with the Python version in `.python-version` and the development tools. `bbench` reads its
inputs from `configs/` in the checkout it was imported from, or from `$BBENCH_ROOT` when that is set. Other pages write
`bbench` without `uv run`; activate `.venv` or add the prefix.

Build the load bot:

```shell
uv run bbench bots build
```

This builds `loadbot`, the bot for every server except PocketMine-MP and Allay, into `bots/bin/<os>-<arch>/`. Those
two need `legacy1001`, the protocol 1001 bot, built with `--module legacy1001` once its gophertunnel fork tag is
published ([bots/README.md#protocol-1001](../bots/README.md#protocol-1001)). On a released version,
`uv run bbench bots fetch` downloads the matching release binary instead and checks it against the release's
`SHA256SUMS`.

Downloads and installs go to the cache: `%LOCALAPPDATA%\bedrock-bench` on Windows, `~/.cache/bedrock-bench` on Linux
(or `$XDG_CACHE_HOME/bedrock-bench`), or `$BBENCH_CACHE` when set. Results go to `runs/` in the working directory.

## Preparing the machine

```shell
uv run bbench doctor
```

`doctor` prints the CPU topology, the placement it would use, the power setting, whether the machine lock is free, the
foreign load over `idle_check_s` (15 s by default), the server's UDP ports and the Go version. It exits with 3 when a
check fails. `--json` prints the same report as JSON.

Before a measured run:

- **Quiet machine.** Close browsers, chat clients, game launchers and any Minecraft client. Stop other benchmarks and
  builds. The harness measures foreign load before every run and refuses to start when it is too high. A run with too
  much foreign load in a measured window is `invalid`.
- **Power.** On Windows, use the High performance or Ultimate Performance plan and keep the machine on mains power. On
  Linux, use the `performance` governor. Turbo and frequency scaling can stay on, but must be the same for every arm.
- **Thermals.** A laptop or a small case may throttle during long runs. If results drift from one repeat to the next,
  watch the CPU temperature and clock speed during a run.
- **CPUs.** The bots need at least 4 logical CPUs outside the server's set. With the default gates, a run whose bot
  CPUs are more than 85% busy is `invalid`. Never put the server on efficiency cores, and never give the bots SMT
  siblings of server CPUs. `doctor` warns about both.
- **Microsoft Defender.** Real-time scanning of the cache and the run directory costs CPU during runs. Add exclusions
  for the cache root and `runs/` if your policy allows.
- **Path length on Windows.** Server trees must stay under the 259-character path limit, and fetch refuses an install
  that would not fit. Enabling long paths in Windows does not help every program a server runs. The deepest BDS file
  is 124 characters, so keep the cache root under about 115: if your user profile path is long, set `BBENCH_CACHE` to
  a short directory such as `C:\bb`.
- **Loopback exemption on Windows.** Before every start, Endstone's launcher checks that the Minecraft app may connect
  to loopback and asks for elevation if not, which stalls an unattended run. For a matrix with an Endstone arm, add the
  exemption once from an elevated prompt, as [its server page](../configs/servers/endstone/README.md#setup) shows.
- **Priority on Linux.** The `endstone-0.11.14` matrices run servers at `above_normal`, which on Linux needs root,
  `CAP_SYS_NICE` or a raised `RLIMIT_NICE`. Any Windows user may set it.
- **One benchmark at a time.** Every run holds the machine lock, `machine.lock` in the cache. Benchmarks that share a
  machine must share the lock: keep the default cache, or point `BBENCH_LOCK` at one common file.

Virtual machines, WSL2 and cloud instances can only pin virtual CPUs. `doctor` warns, runs are recorded with placement
`vcpu`, and the report says not to compare them with bare-metal runs. They are fine for development. On shared cloud
hosts, noisy neighbours show up as run-to-run variance that the foreign-load check cannot see, so use dedicated hosts.

[docs/methodology.md#placement-and-interference](methodology.md#placement-and-interference) explains how placement is
applied and checked.

## Machine profile

A machine profile is a TOML file that says which CPUs the server, the bots and the harness get, at which priority, and
the interference limits every run must stay within. Without `--machine`, the built-in defaults apply: the values in the
comments below, with `auto` CPU sets.

`auto` resolves like this:

- On a hybrid CPU, the server gets every CPU of the fastest core class and the bots get the rest, if that leaves the
  bots at least 4 logical CPUs.
- Otherwise, the bots get whole physical cores, slowest class and highest numbered first, until they have a quarter of
  the cores and at least 4 logical CPUs, but never more than half the cores. The server gets the rest. SMT siblings stay
  together.

For explicit CPU lists, copy this example to a file such as `local/my-machine.toml` and edit it. Every key is optional.
Any file in the checkout that git does not track or ignore marks every run `dirty_harness`, and `--publish` refuses
those runs. `local/` is ignored, so keep machine profiles and claims files there.

```toml
# Intel Core i7-14700F: 8 performance cores with SMT (CPUs 0-15), 12 efficiency cores (CPUs 16-27).

[placement]
server_cpus = "0-15"  # default "auto"; or a list such as "0-7,16"
bot_cpus = "16-27"  # default "auto"; next to an explicit server_cpus, every other CPU; at least 4 or doctor fails
harness_cpus = "bot"  # default "bot": share the bot CPUs; or a list away from the server
priority = "normal"  # normal | above_normal; a matrix's priority_override replaces it for every arm

[runtime]
port = 19672  # the server's IPv4 UDP port; IPv6 uses the next one
jvm_heap = "8G"  # -Xms and -Xmx for every JVM server

[gates]
idle_check_s = 15.0  # seconds of foreign load measured before each run
foreign_cores_mean = 0.25  # mean foreign load in cores, before the run and in every measured window
foreign_cores_peak_10s = 1.5  # highest 10 s mean of foreign load
foreign_server_set_cores_mean = 0.25  # mean foreign load on the server's CPUs
bot_cpu_frac_max = 0.85  # busiest the bot CPUs may be in a measured window
```

Check what it resolves to:

```shell
uv run bbench doctor --machine local/my-machine.toml
```

- The machine label is the file name without `.toml` (`my-machine`), unless the file sets `label`. It is recorded in
  `run.json`, and a claim that names a machine applies only to runs with that label.
- Every arm of a matrix runs with the same profile (rule R3, [docs/methodology.md#rules](methodology.md#rules)).
  `bbench run` refuses to add runs to a results directory that holds runs made with another profile or on another
  machine, so a changed profile needs another `--out` directory. The label and the port are not part of that check.
- [`.github/ci-machine.toml`](../.github/ci-machine.toml) loosens the gates for CI wiring checks. Never measure with it.

## First run

This checks the whole chain with vanilla BDS and 4 bots, in about 15 minutes:

```shell
uv sync
uv run bbench doctor
uv run bbench bots build --module loadbot
uv run bbench fetch bds@1.26.52.3 --accept-eula
uv run bbench smoke bds@1.26.52.3
```

- `fetch` downloads the pinned BDS archive from Mojang, checks it against the sha256 in
  [`configs/servers/bds/server.toml`](../configs/servers/bds/server.toml) and unpacks it into the cache.
  `--accept-eula` (or `BBENCH_ACCEPT_MINECRAFT_EULA=1`) accepts the
  [Minecraft End User License Agreement](https://www.minecraft.net/eula). Nothing from Mojang is stored in this
  repository. `--artifact-dir DIR` takes files with their pinned names from DIR instead of downloading them, for
  example an archive Mojang no longer serves.
- `smoke` runs the [`smoke`](../configs/scenarios/smoke.toml) scenario: 4 bots, view distance 4, short phases. It
  passes when the run is `ok`, the server accepts every command the harness sends, and the bots see movement
  corrections where its `server.toml` declares server-authoritative movement. It prints
  `smoke bds@1.26.52.3: passed` and writes the run under `runs/smoke/`.

Any other server is checked the same way, with `uv run bbench smoke <server>@<version>`.

## Running an experiment

An experiment is a matrix in [`configs/matrices/`](../configs/matrices/): which server builds run (the arms), which
scenarios, how many repeats and in which order. [README.md#experiments](../README.md#experiments) lists E1 to E5, what
each answers and how long it takes. For E1:

```shell
uv run bbench fetch endstone-0.11.14 --accept-eula
uv run bbench run endstone-0.11.14 --machine local/my-machine.toml --dry-run
uv run bbench run endstone-0.11.14 --machine local/my-machine.toml
uv run bbench report runs/endstone-0.11.14
```

- `fetch` with a matrix name fetches every arm. `run` also fetches anything missing before its first run, and stops if
  that fails.
- `--dry-run` prints the run order and exits.
- `run` holds the machine lock for the whole matrix. It runs every arm as many times as the matrix says, in the
  matrix's order (rotated in every shipped matrix), with a cooldown between runs. An `invalid` run is rerun once, and
  `server_failed` is a result that is never rerun. At the end `run` writes the report. `bbench report` rebuilds it from
  the run directories at any time.
- A `spread100` run takes about 10 to 20 minutes, most of it waiting for the views to settle. The phase lengths are in
  [`configs/scenarios/spread100.toml`](../configs/scenarios/spread100.toml).
- An experiment runs on one machine and one OS; it is not repeated per platform. The repeats (3, rotated) all run on
  that machine.
- LeviLamina runs on Windows only, so E3 and E4 are Windows experiments. On Linux, `--only` can run the other arms of
  the cross-server matrix, but that partial run is not E3.

The cross-server matrix has two extra steps. PocketMine-MP and Allay need the protocol 1001 bot, and Nukkit, Nukkit-MOT
and PowerNukkitX also run `spread100-pregen`, which needs a world template built first:

```shell
uv run bbench bots build --module legacy1001
uv run bbench prepare-world nukkit@b3caad6 --scenario spread100-pregen
uv run bbench prepare-world nukkitmot@9cb0aba --scenario spread100-pregen
uv run bbench prepare-world powernukkitx@3.0.5 --scenario spread100-pregen
uv run bbench prepare-world powernukkitx@nightly.86f70bc --scenario spread100-pregen
```

`legacy1001` builds only once its gophertunnel fork tag is published ([bots/README.md](../bots/README.md#patches)).
Until then, `bbench run cross-server` stops at the PocketMine-MP arm, so run the other arms with one `--only` per arm:

```shell
uv run bbench run cross-server --machine local/my-machine.toml --only bds@1.26.52.3 --only endstone@0.11.14 \
  --only bds@1.26.51.1 --only levilamina@26.51.6 --only altay@5.44.7 --only nukkit@b3caad6 \
  --only nukkit@b3caad6+no-watchdog --only nukkitmot@9cb0aba --only nukkitmot@9cb0aba+no-watchdog \
  --only powernukkitx@3.0.5 --only powernukkitx@nightly.86f70bc --only dragonfly@1793369
```

Other `run` options:

- `--repeats N` overrides the matrix's repeat count. Publication needs at least 3 `ok` runs per cell (arm and scenario)
  ([docs/methodology.md#publishing-and-claims](methodology.md#publishing-and-claims)).
- `--order` takes `rotate`, `sequential` or `seeded:N`.
- `--out DIR` writes results under DIR instead of `runs/`.

`run-one` runs one arm once, for example
`uv run bbench run-one --matrix endstone-0.11.14 --arm endstone@0.11.14 --scenario spread100`.

## Reading the output

A matrix writes one directory per run and the aggregate next to them:

```
runs/endstone-0.11.14/
  <run id>/        one run: run.json, metrics.json, server.log.gz, bots.jsonl.gz, series/, config/, crash/
  summary.json     every metric aggregated over the repeats
  report.md        the rendered report
```

The run id is `<UTC time>-<arm>-<scenario>-r<repeat>-<4 hex>`, and run directories are never overwritten. `run.json`
records what ran (artifact hashes, config changes, scenario, machine, placement, interference) and the run's status.
`metrics.json` holds every computed metric. [docs/methodology.md#results-files](methodology.md#results-files) has the
full layout and file formats, and [docs/metrics.md](metrics.md) defines every metric.

Every run ends with one status. `run.json` lists the reasons in `status_reasons`.

| Status | Meaning |
|---|---|
| `ok` | a result |
| `server_failed` | the server crashed, stopped or could not take every player; a result, shown as `k/n ok` |
| `invalid` | the machine or the harness failed, for example foreign load, busy bot CPUs or a lost probe; not a result |

`bbench` exits with one of these codes. A matrix exits with the worst code of its runs.

| Code | Meaning |
|---|---|
| `0` | every run ok |
| `2` | usage or config error, or a report that `--publish` refuses |
| `3` | an environment check refused to start |
| `4` | a server failed |
| `5` | a run was invalid |
| `130` | interrupted |

Read [docs/methodology.md#reading-a-report](methodology.md#reading-a-report) before comparing servers. In short:

- 20 TPS is a ceiling, not a score. Two servers at 20 TPS differ in headroom, shown by tick-thread CPU per tick.
- Read the parity rows before the cost rows. A server that does less work is cheaper for that reason.
- The same workload is not the same work. Compare chunks sent, corrections and packets per player.
- Look at ranges, not points. "A better than B" is printed only when every run of A beats every run of B.

To recompute, check or publish:

```shell
uv run bbench analyze runs/endstone-0.11.14/<run id>
uv run bbench validate runs/endstone-0.11.14
uv run bbench report runs/endstone-0.11.14 --claims local/my-claims.toml
uv run bbench report runs/endstone-0.11.14 --publish
```

`analyze` recomputes `metrics.json` and the status from the raw files. `validate` checks every run against the schemas
in `bedrock_bench/schemas/` and every raw file against its hash in `run.json`. `--claims` checks a claims file against
the report, and `--publish` applies the publication rules; both are described in
[docs/methodology.md#publishing-and-claims](methodology.md#publishing-and-claims).

To send results or report a problem, see
[.github/CONTRIBUTING.md#reporting-a-problem](../.github/CONTRIBUTING.md#reporting-a-problem).
