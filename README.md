# bedrock-bench

Reproducible, fair load benchmarks for Minecraft Bedrock servers.

bedrock-bench starts a server, connects headless bots to it, spreads them across a fresh world so that no two share a
chunk, and measures how the server copes: tick rate, tick-thread CPU per tick, process CPU and memory, how deep TPS
falls while views load and how long it takes to recover, and how long each player waits for its view. Every server goes
through the same phases, samplers, bot and metric code. Only a small adapter per server knows its name.

Results come with what the server actually did: its declared feature parity with vanilla, every config change from its
defaults with a reason, and observed work counters such as movement corrections and chunks sent per player. There is
no composite score.

> **Status:** v0.1 is under construction. `legacy/` holds the scripts behind Endstone's
> [BENCHMARK.md](https://github.com/EndstoneMC/endstone/blob/main/BENCHMARK.md) for 0.11.14 and is removed once the
> new harness replaces it. [`published/legacy-0.11.14/PROVENANCE.md`](published/legacy-0.11.14/PROVENANCE.md) traces
> each published figure back to its run.

## What it measures

| Area | Examples | Details |
|---|---|---|
| Tick rate | steady TPS, lowest 5 s and 15 s TPS while views load, ticks lost, time below 19.5, recovery time | [METRICS](docs/METRICS.md) |
| Headroom | tick-thread CPU per executed tick, tick-thread busy fraction, saturated seconds | [ADR 0003](docs/adr/0003-tick-cpu-is-not-mspt.md) |
| Cost | process cores, memory (private bytes on Windows, RSS on Linux) | [METRICS](docs/METRICS.md) |
| Player experience | time to first chunk, time to the 80th chunk in view, view fill | [METRICS](docs/METRICS.md) |
| Work done | movement corrections, actor moves, block updates, locator packets, chunks per player | [READING](docs/READING.md) |
| Robustness | seeded chaos: churn, kicks, kills, teleports, dimension changes, tick-speed changes | [WORKLOADS](docs/WORKLOADS.md) |

TPS comes only from a counter that advances once per executed tick, read from the server console every 5 seconds.
The harness never hooks into a server; it reads what the server prints and what the operating system reports about
the process.

## Servers

| Adapter | Server | Windows | Linux | Docker |
|---|---|:-:|:-:|:-:|
| `bds` | Bedrock Dedicated Server (vanilla) | yes | yes | yes |
| `endstone` | [Endstone](https://github.com/EndstoneMC/endstone), installed from PyPI | yes | yes | yes |
| `levilamina` | [LeviLamina](https://github.com/LiteLDev/LeviLamina) | yes | no | no |
| `pocketmine` | [PocketMine-MP](https://github.com/pmmp/PocketMine-MP) | yes | yes | yes |
| `nukkit` | [Nukkit](https://github.com/CloudburstMC/Nukkit) | yes | yes | yes |
| `powernukkitx` | [PowerNukkitX](https://github.com/PowerNukkitX/PowerNukkitX) | yes | yes | yes |
| `dragonfly` | [Dragonfly](https://github.com/df-mc/dragonfly) | yes | yes | yes |

Per-server notes, pinned versions and every config override are in [`docs/servers/`](docs/servers/). Adding a server
takes one adapter file, a template folder, a feature file and an artifact file:
[docs/ADDING_A_SERVER.md](docs/ADDING_A_SERVER.md).

## Requirements

- Windows 10/11 x64 or Linux x86_64. macOS can analyze and report but not run servers.
- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). The only runtime dependency is `psutil`.
- Go 1.25 or newer to build the load bot, or `bbench bots fetch` to download a release binary.
- At least 4 logical CPUs for the bots on top of the server's CPU set.
- Docker is optional ([docs/PLATFORMS.md](docs/PLATFORMS.md)).

## Quick start

```shell
uv sync
uv run bbench doctor                                   # topology, power plan, idle load, toolchains
uv run bbench fetch endstone-0.11.14 --accept-eula     # pinned downloads, sha256-verified
uv run bbench bots build                               # load bot from source
uv run bbench smoke bds@1.26.52.3                      # 4 bots, short phases, strict checks
uv run bbench run endstone-0.11.14                     # the headline matrix: 3 repeats, rotated order
uv run bbench report runs/endstone-0.11.14             # summary.json + report.md
```

`--accept-eula` (or `BBENCH_ACCEPT_MINECRAFT_EULA=1`) accepts the
[Minecraft End User License Agreement](https://www.minecraft.net/eula) for the BDS download. Nothing from Mojang is
stored in this repository.

A 100-bot run takes 10 to 20 minutes, most of it spent waiting for the views to settle. The headline matrix runs
three servers three times each. Use a quiet machine: the harness takes a machine lock, checks for foreign load before
every run and marks a run `invalid` if something else competed for the CPUs while it measured.

Exit codes: `0` all runs ok, `2` usage or config error, `3` an environment check refused to start, `4` a server
failed (crashed, stopped or could not take all players), `5` a run was invalid, `130` interrupted.

## Reading results

Read [docs/READING.md](docs/READING.md) before comparing servers. In short:

- 20 TPS is a ceiling, not a score. Two servers at 20 TPS differ in headroom, shown by tick-thread CPU per tick.
- Read the parity rows before the cost rows. A server that does less work is cheaper for a reason.
- Same workload is not same work: compare chunks sent, corrections and packets per player.
- Look at ranges, not points. "A better than B" is printed only when every run of A beats every run of B.
- A crash or a server that cannot take every player is a result, shown as `k/n ok`.

## Documentation

| Document | Content |
|---|---|
| [METHODOLOGY](docs/METHODOLOGY.md) | rules, phases, settle rule, windows, placement, statistics, validity gates, limitations |
| [METRICS](docs/METRICS.md) | every metric with unit, window and formula (generated) |
| [READING](docs/READING.md) | how to read a report |
| [FAIRNESS](docs/FAIRNESS.md) | fairness controls, config parity per server, override categories |
| [FEATURES](docs/FEATURES.md) | declared feature parity per server with evidence (generated) |
| [WORKLOADS](docs/WORKLOADS.md) | scenarios, grid, bot behaviour, chaos actions |
| [PITFALLS](docs/PITFALLS.md) | every trap we hit and how the harness handles it |
| [PLATFORMS](docs/PLATFORMS.md) | Windows, Linux, Docker, hybrid CPUs, WSL2 |
| [RESULTS_SCHEMA](docs/RESULTS_SCHEMA.md) | run directory, file formats, schemas, versioning |
| [PROFILING](docs/PROFILING.md) | profiling outside measured runs |
| [REPRODUCING-endstone-0.11.14](docs/REPRODUCING-endstone-0.11.14.md) | checking the figures in Endstone's BENCHMARK.md for 0.11.14 |
| [ADDING_A_SERVER](docs/ADDING_A_SERVER.md) | adapter walkthrough and checklist |
| [adr/](docs/adr/) | design decisions |
| [bots/README](bots/README.md) | bot flags, events, build, patch policy |

## Repository layout

```
src/bedrock_bench/   harness, samplers, metrics, report, adapters
bots/                Go load bot (core behaviour + one frontend per protocol)
servers/             config templates and helper plugin sources per server (no binaries)
scenarios/           workloads (one file drives every arm of a matrix)
matrices/            which builds run which scenarios, how often, in which order
machines/            CPU placement, priority, interference gates
artifacts/           pinned downloads with sha256
features/            declared feature parity per server
claims/              relational claims checked against a report
schemas/             JSON schemas of every result file
published/           published results and their provenance
docs/                documentation
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). An adapter pull request needs a passing `bbench smoke`, a feature file with
evidence for every cell, and a page in `docs/servers/`.

## License

Code and documentation: [Apache-2.0](LICENSE). Published result datasets under `published/`:
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Third-party notices: [NOTICE](NOTICE).

Not affiliated with Mojang Studios or Microsoft. "Minecraft" and "Bedrock" are trademarks of Mojang Studios and
Microsoft.
