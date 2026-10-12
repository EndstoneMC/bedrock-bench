# bedrock-bench

bedrock-bench is a load benchmark for Minecraft Bedrock servers. It installs a server from a pinned, hash-checked
download, connects headless bots, spreads them across a fresh world so that no two share a chunk, and measures how the
server copes: tick rate, tick-thread CPU per tick, process CPU and memory, how far TPS falls while views load and how
long each player waits for its view. Every server goes through the same phases, bot, samplers and metric code, and a
small adapter per server holds the rest. Each result comes with the server's declared feature parity with vanilla and
every config change from its defaults, with a reason. There is no composite score. bedrock-bench is maintained by the
Endstone project, one of the servers it measures; every input and rule is in this repository so anyone can check them.

## Supported servers

Each server has one folder under [`configs/servers/`](configs/servers/). Its `server.toml` holds the pinned downloads
with their sha256, the declared feature parity and every config change the harness makes. Its README says how a run
starts and what to watch for.

| Server | Language | Pinned versions | Config | Notes |
|---|---|---|---|---|
| Bedrock Dedicated Server | C++ | `bds@1.26.52.3`<br>`bds@1.26.51.1` | [`configs/servers/bds/`](configs/servers/bds/) | The vanilla reference. The download needs the Minecraft EULA. |
| [Endstone](https://github.com/EndstoneMC/endstone) | C++ on BDS | `endstone@0.11.13`<br>`endstone@0.11.14` | [`configs/servers/endstone/`](configs/servers/endstone/) | Installed from PyPI with hash-locked dependencies. Runs inside `bds@1.26.52.3`. Variant `glibc-alloc` (Linux only). |
| [LeviLamina](https://github.com/LiteLDev/LeviLamina) | C++ on BDS | `levilamina@26.51.6` | [`configs/servers/levilamina/`](configs/servers/levilamina/) | Windows only. Runs inside `bds@1.26.51.1`. Two packages need the LeviMC EULA (`--accept-eula`). Variants `levioptimize` and `levioptimize-all`. |
| [PocketMine-MP](https://github.com/pmmp/PocketMine-MP) | PHP | `pocketmine@5.44.3` | [`configs/servers/pocketmine/`](configs/servers/pocketmine/) | Speaks protocol 1001, so it needs the [protocol 1001 bot](bots/README.md#protocol-1001). Variant `stock`. |
| [Altay](https://github.com/altayofficial/Altay) | PHP | `altay@5.44.7` | [`configs/servers/altay/`](configs/servers/altay/) | Fork of PocketMine-MP 5 that speaks protocol 2193. Variant `stock`. |
| [Nukkit](https://github.com/CloudburstMC/Nukkit) | Java | `nukkit@b3caad6` | [`configs/servers/nukkit/`](configs/servers/nukkit/) | Snapshot build, versioned by commit. Variant `no-watchdog`. |
| [Nukkit-MOT](https://github.com/MemoriesOfTime/Nukkit-MOT) | Java | `nukkitmot@9cb0aba` | [`configs/servers/nukkitmot/`](configs/servers/nukkitmot/) | Jenkins build 1592, versioned by commit. Jenkins keeps few builds, so keep a copy of the jar for `--artifact-dir`. Variant `no-watchdog`. |
| [PowerNukkitX](https://github.com/PowerNukkitX/PowerNukkitX) | Java | `powernukkitx@3.0.5`<br>`powernukkitx@nightly.86f70bc` | [`configs/servers/powernukkitx/`](configs/servers/powernukkitx/) | The nightly runs on JDK 25. Upstream replaces the nightly jar with every build, so keep a copy for `--artifact-dir`. |
| [Allay](https://github.com/AllayMC/Allay) | Java | `allay@0.14.0` | [`configs/servers/allay/`](configs/servers/allay/) | Speaks protocol 1001, so it needs the [protocol 1001 bot](bots/README.md#protocol-1001). Flat worlds only. |
| [Dragonfly](https://github.com/df-mc/dragonfly) | Go | `dragonfly@1793369` | [`configs/servers/dragonfly/`](configs/servers/dragonfly/) | `bbench fetch` builds it from source, so it needs Go 1.26 or newer. The one server CI runs. |
| [Chorus](https://github.com/bedrock-crustaceans/chorus) | Rust | `chorus@95ca835` | [`configs/servers/chorus/`](configs/servers/chorus/) | `bbench fetch` builds it from source, so it needs Rust 1.95.0. Its player list gives the bots decode errors, so every run at this commit is invalid and it is not in E3. |

Evaluated, not supported: [MiNET](https://github.com/NiclasOlofsson/MiNET) (needs a NetherNet bot),
[JSPrismarine](https://github.com/JSPrismarine/JSPrismarine) (protocol 748, client-authoritative movement),
[SerenityJS](https://github.com/SerenityJS/serenity) (protocol 975, rejects the bots' login and stalls without console
input) and Sculk (discontinued, a login and chat skeleton with no world).

PocketMine-MP, Altay, Nukkit, Nukkit-MOT, PowerNukkitX and Allay run on the PHP and JDK builds pinned in
[`configs/servers/runtimes.toml`](configs/servers/runtimes.toml). To add a server, see
[.github/CONTRIBUTING.md](.github/CONTRIBUTING.md#adding-a-server).

## Installation

bedrock-bench runs servers on Windows 10/11 x64 and Linux x86_64. macOS can analyze and report but not run servers.
Measured runs need bare metal; runs on a virtual machine or WSL2 are recorded as `vcpu` and are not comparable.

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). The only runtime dependency is psutil.
- Go 1.25 or newer to build the load bots, and 1.26 or newer for Dragonfly. On a released version, `bbench bots fetch`
  downloads the load bot instead and checks it against the release's `SHA256SUMS`.
- Rust 1.95.0 through [rustup](https://rustup.rs/) and a C compiler, only to fetch Chorus.
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
uv run bbench doctor                              # CPU topology, power plan, idle load, toolchains, machine lock
uv run bbench bots build --module loadbot         # the load bot, from source
uv run bbench fetch bds@1.26.52.3 --accept-eula   # pinned download, checked against its sha256
uv run bbench smoke bds@1.26.52.3                 # 4 bots, short phases, strict checks
```

`--accept-eula` (or `BBENCH_ACCEPT_MINECRAFT_EULA=1`) accepts the
[Minecraft End User License Agreement](https://www.minecraft.net/eula) for the BDS download. Nothing from Mojang is
stored in this repository. The smoke run is written to `runs/smoke/`, and
[docs/get_started.md](docs/get_started.md#reading-the-output) explains what is in it.

## Experiments

An experiment is a matrix file in [`configs/matrices/`](configs/matrices/): the server builds (arms), the scenarios they
run, three repeats of each in rotated order and a 60 s cooldown between runs. Within a matrix, every arm gets the same
scenario files, bot flags and CPU placement.

| | Question | Matrix | Servers | Scenario | Runs | Time |
|---|---|---|---|---|---|---|
| E1 | Endstone against the BDS build it hosts, client-side generation off | [`endstone-0.11.14.toml`](configs/matrices/endstone-0.11.14.toml) | `bds@1.26.52.3`, `endstone@0.11.13`, `endstone@0.11.14` | [`spread100`](configs/scenarios/spread100.toml) | 3 × 3 | 1.5–3 h |
| E2 | E1 with client-side generation on, the setting of the blog's figures | [`endstone-0.11.14-csg.toml`](configs/matrices/endstone-0.11.14-csg.toml) | as E1 | [`spread100-csg`](configs/scenarios/spread100-csg.toml) | 3 × 3 | 1.5–3 h |
| E3 | Every server on the same workload | [`cross-server.toml`](configs/matrices/cross-server.toml) | every server but Chorus, 14 arms | [`spread100`](configs/scenarios/spread100.toml), [`spread100-pregen`](configs/scenarios/spread100-pregen.toml) | 20 × 3 | 11–21 h |
| E4 | LeviLamina and LeviOptimize against the BDS build they host | [`levilamina-26.51.toml`](configs/matrices/levilamina-26.51.toml) | `bds@1.26.51.1`, `levilamina@26.51.6` and its 2 variants | [`spread100`](configs/scenarios/spread100.toml) | 4 × 3 | 2–4 h |
| E5 | Robustness under 15 minutes of seeded chaos, never ranked | [`endstone-0.11.14-chaos.toml`](configs/matrices/endstone-0.11.14-chaos.toml) | as E1 | [`chaos`](configs/scenarios/chaos.toml) | 3 × 3 | 4–5.5 h |

Runs are cells (arm and scenario) times repeats. Times follow from the phase lengths in the scenario files: a spread100
run takes 10 to 20 minutes, most of it waiting for the views to settle, and chaos adds 15 minutes. The cooldowns are
included; downloads, installs and the E3 world templates are not.

```shell
uv run bbench fetch endstone-0.11.14 --accept-eula   # every pin the matrix needs
uv run bbench run endstone-0.11.14 --dry-run         # print the run order
uv run bbench run endstone-0.11.14 --machine local/my-machine.toml
uv run bbench report runs/endstone-0.11.14           # summary.json and report.md
```

`bbench run` writes the report when it finishes, and `bbench report` rebuilds it from the run directories. Without
`--machine`, built-in defaults apply and the CPU sets are chosen automatically
([docs/get_started.md](docs/get_started.md#machine-profile)).

An experiment runs on one machine and one OS; it is not repeated per platform. The repeats (3, rotated) all run on
that machine. A report never mixes machines, and `bbench run` refuses to add runs from another machine or machine
profile to an existing results directory. Keep machine profiles and claims files in `local/`, which git ignores: any
other untracked or changed file in the checkout marks every run `dirty_harness`, and `--publish` refuses those runs.

- E3: build the world templates first with `uv run bbench prepare-world nukkit@b3caad6 --scenario spread100-pregen` and
  the same for `nukkitmot@9cb0aba`, `powernukkitx@3.0.5` and `powernukkitx@nightly.86f70bc`. Reports group arms by
  declared fidelity and never rank across groups.
- E3: the PocketMine-MP and Allay arms need the protocol 1001 bot, which builds only once its gophertunnel fork tag is
  published ([bots/README.md](bots/README.md#patches)). Until then E3 cannot run in full: `bbench run cross-server`
  stops at the first of those arms. [docs/get_started.md](docs/get_started.md#running-an-experiment) shows how to run
  the other arms.
- E3 and E4 need Windows, because LeviLamina runs on Windows only.
- E5 runs on its own, so it does not repeat E1. [`chaos-portable`](configs/scenarios/chaos-portable.toml) has the
  actions every server supports.

Read [docs/methodology.md](docs/methodology.md#reading-a-report) before comparing servers.
[docs/metrics.md](docs/metrics.md) gives every metric's unit, window and formula.

## Why trust the numbers

- **One source for TPS.** TPS comes only from a counter that advances once per executed tick, read from the server
  console every 5 s: `time query gametime` on BDS-hosted servers and PowerNukkitX, a helper's `gametime` command
  elsewhere ([rule R8](docs/methodology.md#rules), `Dialect.tick_cmd` in `bedrock_bench/adapters/base.py`).
- **Pinned, hashed inputs.** `bbench fetch` checks every download against the sha256 in its `server.toml`. Each
  `run.json` records the install tree hash, the server executable's hash before and after the run, each config file
  as written and after shutdown, the bot binary, the scenario hash and a machine fingerprint
  (`bedrock_bench/schemas/run.schema.json`).
- **Every config change has a reason.** `server.toml` files each change under a category (`harness`, `parity`,
  `rescue` or `nonvanilla`) with a reason, `run.json` records it, and the report footnotes every `rescue` and
  `nonvanilla` change ([rule R6](docs/methodology.md#rules), `bedrock_bench/report/footnotes.py`). Cost tables sit
  next to work counters and feature parity (rule R10, `bedrock_bench/report/markdown.py`).
- **A busy machine voids the run.** The harness takes a machine lock, refuses to start on a machine that is not idle,
  and marks a run `invalid` when foreign load reaches a measured window ([rule R4](docs/methodology.md#rules);
  `tests/integration/test_contamination.py` burns a server CPU mid-run to check it). Crashes and join failures are
  results, shown as `k/n ok`, and are never dropped (rule R9, `tests/integration/test_death.py`).
- **No composite score; "better" needs complete separation.** A report calls one arm better only when every run of it
  beats every run of the other, at p ≤ 0.05 (`separated` in `bedrock_bench/report/aggregate.py`).
  `bbench report --publish` refuses fewer than 3 ok runs per cell, local artifacts, unofficial runs, unknown parity and
  client-side generation in a cross-server table (`publish_problems`,
  [publication rules](docs/methodology.md#publishing-and-claims)).
- **CI re-checks the harness.** Synthetic fixture runs (`tests/fixtures/runs/`) must recompute to byte-identical
  `metrics.json` (`tests/integration/test_recompute_golden.py`), the bots built on Linux and on Windows must be
  identical (`.github/workflows/bots.yml`), and a real Dragonfly server passes a 4-bot smoke on every harness change
  (`.github/workflows/e2e-dragonfly.yml`).

The full method, its design decisions and its threats to validity are in [docs/methodology.md](docs/methodology.md).

## The story

The [blog](docs/blog/README.md) tells the story of the Endstone 0.11.14 performance work this benchmark grew out of.
Its figures come from single runs of earlier scripts with client-side generation on, the setting of E2.

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
