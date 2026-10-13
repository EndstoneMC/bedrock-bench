# bedrock-bench

bedrock-bench is a load benchmark for Minecraft Bedrock servers. It installs a server from a pinned, hash-checked
download, connects headless bots spread out across a fresh world, and measures how the server copes: tick rate,
tick-thread CPU per tick, process CPU and memory, how far TPS falls while the players' views load, and how long each
player waits for its view. Every server gets the same workload, bots, phases and metric code. A small adapter per
server starts it, reads its tick counter and stops it. There is no composite score.

## Supported servers

Each server has one folder under [`servers/`](servers/). Its `server.toml` pins the downloads with their sha256 and
names the version `bbench run` uses by default. Next to it are the config templates the harness renders, named like
the real files, any helper the server needs, and a README on how a run starts and what to watch for. Default versions
are in bold.

| Server | Language | Pinned versions | Folder | Notes |
|---|---|---|---|---|
| Bedrock Dedicated Server | C++ | **`bds@1.26.52.3`** | [`servers/bds/`](servers/bds/) | The vanilla reference. The download needs the Minecraft EULA. |
| [Endstone](https://github.com/EndstoneMC/endstone) | C++ on BDS | **`endstone@0.11.14`** | [`servers/endstone/`](servers/endstone/) | Runs inside `bds@1.26.52.3` on Python 3.13. Installed from PyPI with hash-locked dependencies. |
| [LeviLamina](https://github.com/LiteLDev/LeviLamina) | C++ on BDS | **`levilamina@26.51.6`** | [`servers/levilamina/`](servers/levilamina/) | Windows only. Runs inside BDS 1.26.51.1, pinned with LeviLamina. Two of its packages need the LeviMC EULA. |
| [PocketMine-MP](https://github.com/pmmp/PocketMine-MP) | PHP | **`pocketmine@5.44.3`** | [`servers/pocketmine/`](servers/pocketmine/) | Speaks protocol 1001, so it needs the [protocol 1001 bot](bots/README.md#protocol-1001). |
| [Altay](https://github.com/altayofficial/Altay) | PHP | **`altay@5.44.7`** | [`servers/altay/`](servers/altay/) | Fork of PocketMine-MP 5 that speaks protocol 2193. |
| [Nukkit](https://github.com/CloudburstMC/Nukkit) | Java | **`nukkit@b3caad6`** | [`servers/nukkit/`](servers/nukkit/) | Snapshot build, versioned by commit. Old snapshots get pruned, so keep a copy of the jar for `--artifact-dir`. |
| [Nukkit-MOT](https://github.com/MemoriesOfTime/Nukkit-MOT) | Java | **`nukkitmot@9cb0aba`** | [`servers/nukkitmot/`](servers/nukkitmot/) | Jenkins build 1592, versioned by commit. Jenkins keeps few builds, so keep a copy of the jar for `--artifact-dir`. |
| [PowerNukkitX](https://github.com/PowerNukkitX/PowerNukkitX) | Java | `powernukkitx@3.0.5`<br>**`powernukkitx@nightly.86f70bc`** | [`servers/powernukkitx/`](servers/powernukkitx/) | The nightly runs on JDK 25. Upstream replaces the nightly jar with every build, so keep a copy for `--artifact-dir`. |
| [Allay](https://github.com/AllayMC/Allay) | Java | **`allay@0.14.0`** | [`servers/allay/`](servers/allay/) | Speaks protocol 1001, so it needs the [protocol 1001 bot](bots/README.md#protocol-1001). |
| [Dragonfly](https://github.com/df-mc/dragonfly) | Go | **`dragonfly@1793369`** | [`servers/dragonfly/`](servers/dragonfly/) | Built from source, so it needs Go 1.26 or newer. |
| [Chorus](https://github.com/bedrock-crustaceans/chorus) | Rust | **`chorus@5cffd3`** | [`servers/chorus/`](servers/chorus/) | Built from source with Rust 1.95.0 |

Evaluated, not supported: [MiNET](https://github.com/NiclasOlofsson/MiNET) (needs a NetherNet bot),
[JSPrismarine](https://github.com/JSPrismarine/JSPrismarine) (protocol 748, client-authoritative movement),
[SerenityJS](https://github.com/SerenityJS/serenity) (protocol 975, rejects the bots' login and stalls without console
input) and [Sculk](https://github.com/sculkmp/Sculk) (discontinued, a login and chat skeleton with no world).

PocketMine-MP, Altay, Nukkit, Nukkit-MOT, PowerNukkitX and Allay run on the PHP and JDK builds pinned in
[`servers/runtimes.toml`](servers/runtimes.toml). To add a server, see
[.github/CONTRIBUTING.md](.github/CONTRIBUTING.md#adding-a-server).

## Installation

bedrock-bench runs servers on Windows 10/11 x64 and Linux x86_64. Measure on bare metal: a run on a virtual machine
or WSL2 is recorded as `vcpu` and is not comparable with bare-metal runs.

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
- Go 1.25 or newer to build the load bot, and 1.26 or newer for Dragonfly.
- Rust 1.95.0 through [rustup](https://rustup.rs/) and a C compiler, only for Chorus.
- At least 4 logical CPUs for the bots on top of the CPUs the server gets.

```shell
git clone https://github.com/EndstoneMC/bedrock-bench.git
cd bedrock-bench
uv sync
```

[docs/get_started.md](docs/get_started.md) covers preparing the machine and writing a machine profile.

## Getting started

A first run checks the machine, builds the bot, downloads one BDS build and runs a 4-bot smoke test. It takes about
15 minutes.

```shell
uv run bbench doctor                                   # CPU layout, power plan, idle load, toolchains
uv run bbench bots build                               # the load bot, from source
uv run bbench fetch bds@1.26.52.3 --accept-eula        # pinned download, checked against its sha256
uv run bbench smoke configs/smoke.toml bds@1.26.52.3   # 4 bots, short phases, strict checks
```

`--accept-eula` accepts the [Minecraft End User License Agreement](https://www.minecraft.net/eula) for BDS, and the
LeviMC EULA when it fetches LeviLamina. Nothing from Mojang is stored in this repository. The smoke run is written to
`runs/<session-id>/`, and [docs/get_started.md](docs/get_started.md#reading-the-output) explains what is in it.

## Experimental setup

Every server runs the same workload, set by [`configs/spread-100.toml`](configs/spread-100.toml) and the base it
inherits from, [`configs/spread.toml`](configs/spread.toml).

| Setting | Value |
|---|---|
| Players | 100 bots, starting to join 300 ms apart |
| Spread | One bot per cell of a grid 11 cells wide with 640 blocks between cells, so no two views overlap. Each bot walks a circle about 22 blocks in radius. |
| View distance | 10 chunks |
| Simulation distance | 4 chunks |
| Client-side generation | Off: the server generates and sends every chunk |
| World | New for every run, seed 20261010, survival, easy |
| Phases | Boot, 30 s idle without bots, join, settle while views load (5 to 15 min), 90 s steady window, stop |
| Repeats | 3 per server, in an order rotated each repeat, 60 s pause between runs, an invalid run rerun once |
| CPUs | The server gets one set of CPUs and the bots and harness another, split the same way for every server |

Each run records the machine, the OS and every version it used in its `run.json`. A report refuses to mix machines,
machine profiles or configs.

## Running the benchmark

```shell
uv run bbench fetch --accept-eula            # every server's default version
uv run bbench run configs/spread-100.toml    # every default version, 3 rotated repeats
```

`bbench run` takes the config first. With no server tags it runs every server's default version as one session and
writes it to `runs/<session-id>/`: one directory per run, `report.md`, `summary.json` and the CSV files.
`uv run bbench report runs/<session-id>` renders the report again. A run takes 10 to 20 minutes, most of it waiting
for views to load, so a full session on Windows (8 servers, 24 runs) takes about 4 to 8 hours.

With no tags, `bbench fetch` and `bbench run` skip these servers and say why: Chorus until its player list decodes,
PocketMine-MP and Allay until the protocol 1001 bot can be built, and LeviLamina on Linux. The report lists them.

To run a subset, name the servers:

```shell
uv run bbench run configs/spread-100.toml bds@1.26.52.3 endstone@0.11.14
```

LeviLamina runs inside BDS 1.26.51.1, an older build than `bds@1.26.52.3`, so its row notes that the session has no
vanilla run of that build.

On a machine that cannot drive 100 bots, use [`configs/spread-50.toml`](configs/spread-50.toml) or
[`configs/spread-25.toml`](configs/spread-25.toml), the same setup with fewer bots. Results are comparable only within
one config, and the report names its config and bot count at the top.

Draw the figures from a session's CSV files into [`results/`](results/):

```shell
uv run scripts/plot_results.py runs/<session-id>
```

## Methodology

Every server goes through the same phases with the same bots, samplers and metric code. TPS comes only from a counter
that advances once per executed tick, read from the server console every 5 s, and crashes and failed runs are
reported, never dropped. [docs/methodology.md](docs/methodology.md) gives the rules and their limits, including a
[conflict-of-interest note](docs/methodology.md#conflict-of-interest): EndstoneMC maintains this benchmark and also
develops Endstone. [docs/metrics.md](docs/metrics.md) defines every metric.

## The story

The [blog](docs/blog/README.md) tells the story of the Endstone 0.11.14 performance work this benchmark grew out of.

## Citation

```bibtex
@software{bedrock_bench,
  author = {{EndstoneMC}},
  title  = {bedrock-bench: a load benchmark for Minecraft Bedrock servers},
  year   = {2026},
  url    = {https://github.com/EndstoneMC/bedrock-bench}
}
```

Please also cite the harness version your runs record (`harness.git` in `run.json`).

## License

Code and documentation are under [Apache-2.0](LICENSE). Third-party notices are in [NOTICE](NOTICE).

Not affiliated with Mojang Studios or Microsoft. "Minecraft" and "Bedrock" are trademarks of Mojang Studios and
Microsoft.
