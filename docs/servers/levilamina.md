# LeviLamina (`levilamina`)

[LeviLamina](https://github.com/LiteLDev/LeviLamina) is a mod loader for Bedrock Dedicated Server on Windows. It
runs Mojang's BDS binary of a fixed build: a patched copy of the executable loads PreLoader, which loads LeviLamina
and its mods before the server starts. Gameplay is BDS's own. LeviLamina also routes the server's memory allocations
through mimalloc and drops some malformed packets, so its parity rows (`features/levilamina.toml`) are fidelity V1.
[LeviOptimize](https://github.com/LiteLDev/LeviOptimize), a performance mod for LeviLamina, is measured as two
variants.

Adapter: `src/bedrock_bench/adapters/levilamina.py`, built on the shared BDS behaviour in `adapters/bdsfamily.py`.
Configuration notes: `servers/levilamina/README.md`.

## Versions

| Version | Hosts BDS | Packages | Allocator |
|---|---|---|---|
| 26.51.6 | 1.26.51.1 | PreLoader 1.16.3, PeEditor 3.9.2, CrashLogger 1.3.3, bedrock-runtime-data 26.51.1-server.7; LeviOptimize 0.17.0 for the variants | mimalloc, always |

LeviLamina 26.51.6 depends on BDS 1.26.51, so `matrices/levilamina-26.51.toml` compares it with `bds@1.26.51.1`, the
build it hosts. In a matrix without that build, for example next to `endstone@0.11.14` and `bds@1.26.52.3` only, the
report flags it `different_bds_build`. Package versions are the ones LeviLamina's `tooth.json` resolves to for this
release; all are pinned with size and sha256 in `artifacts/levilamina.toml`.

LeviLamina ships for Windows only. The adapter declares `platforms = windows`, and the runner refuses it on Linux and
in Docker.

## Setup

```shell
bbench fetch levilamina@26.51.6 --accept-eula
```

Two licences cover the downloads. The BDS zip is covered by the [Minecraft EULA](https://www.minecraft.net/eula).
PreLoader and PeEditor are covered by the
[LeviMC closed-source software EULA](https://github.com/LiteLDev/PreLoader/blob/v1.16.3/EULA.en.md). `--accept-eula`
(or `BBENCH_ACCEPT_MINECRAFT_EULA=1`) accepts both. Files already in the cache, or supplied through
`--artifact-dir DIR` under their release names, need no flag. Nothing from these downloads is ever committed or
published.

Fetch does three things:

1. Verifies and installs `bds@1.26.51.1` exactly as for vanilla BDS (`docs/servers/bds.md`).
2. Downloads each package's GitHub release asset and checks its size and sha256.
3. Builds the install in the cache. It starts from the BDS tree, places each package's files as that package's own
   `tooth.json` does (table below), and then runs `PeEditor.exe -mb`, the step `lip install` runs. PeEditor writes
   `bedrock_server_mod.exe` and renames the original executable to `bedrock_server.exe.bak`. The install is made
   read-only and its tree hash is checked before every run.

| Package | Release asset | Placed as |
|---|---|---|
| LeviLamina | `levilamina-v26.51.6-server-release-windows-x64.zip` | `LeviLamina/` -> `plugins/LeviLamina/` |
| PreLoader | `preloader-v1.16.3-windows-x64.zip` | `bin/PreLoader.dll` -> `PreLoader.dll` |
| PeEditor | `peeditor-windows-x64.zip` | `PeEditor.exe` |
| CrashLogger | `CrashLogger-windows-x64.zip` | `CrashLogger.exe` -> `plugins/LeviLamina/CrashLogger.exe` |
| bedrock-runtime-data | `bedrock-runtime-data-v26.51.1-server.7-windows-x64.zip` | `bedrock_runtime_data` |
| LeviOptimize (variants only) | `LeviOptimize-server-windows-x64.zip` | `LeviOptimize/` -> `plugins/LeviOptimize/` |

PeEditor runs once, while the install is built, and never in a run. The install's tree hash therefore covers the
executable every run starts, and its sha256 is taken again before and after each run. The base arm and the
LeviOptimize variants get separate installs; the two variants share one. A package that would overwrite a file
placed earlier stops the fetch.

The translations package (`levilamina-loc`) is not installed. It has no release asset, and without it LeviLamina's
messages are in English, their source language, which is also the language the harness parses.

An arm may point `artifact` at a local LeviLamina server zip. It replaces only the LeviLamina package; the other
packages and BDS stay pinned, and the results are marked `unpublished_artifact`.

The deepest file is the same as in vanilla BDS, so the same limit on the cache root length applies on Windows
(`docs/servers/bds.md`).

## How a run starts

```
bedrock_server_mod.exe       measured process, started directly in the run's server copy
  └── CrashLogger.exe        started by LeviLamina, attached to the server
```

CrashLogger attaches to the server as a debugger so it can write a report when the server crashes; LeviLamina skips
it when another debugger is already attached. This is LeviLamina's default setup and stays on. Its own CPU is not
part of `process_cores`. What the attachment costs the server is not separated from the server's own CPU.

## Overrides

Everything vanilla BDS gets (`docs/servers/bds.md`, `servers/bds/properties.toml`), plus:

| File | Key | Generated | Benchmark | Category | Reason |
|---|---|---|---|---|---|
| `plugins/LeviLamina/config/Config.json` | `modules.crashLogger.uploadToSentry` | `true` | `false` | harness | no crash report upload during measurement; reports stay in `logs/crash` |
| `plugins/LeviLamina/config/Config.json` | `targeted.enableStatistics` | `true` | `false` | harness | no usage statistics upload during measurement |
| `plugins/LeviOptimize/manifest.json` | `version` | absent | `0.17.0` | nonvanilla | both variants: the LeviOptimize mod is loaded |
| `plugins/LeviOptimize/config/config.json` | `features.optMovingBlock`, `features.optSeenPercent`, `features.optPushEntity.enable` | `false` | `true` | nonvanilla | `levioptimize-all` only (below) |

"Generated" is what the pinned release writes on its first start. PreLoader's config, LeviLamina's config and, for
the variants, LeviOptimize's config are written into every run from `servers/levilamina`, because LeviLamina merges
an older config over its defaults when the config version changes and LeviOptimize saves its config when the server
stops. After the run each file is read back and compared value by value; a changed value fails `config_readback`.

Left at LeviLamina's defaults, among others (full list in `servers/levilamina/README.md`):

- `targeted.vulnerabilityFixes.*`, all on: oversized sub-chunk requests are dropped, item data is checked more
  strictly, and connection attempts per IP are limited (`limitIPFrequency`). The bots stay within these limits, and
  `dropped == 0` is checked.
- `targeted.forceEnableCheatCommands = true`: cheats are on in the world. The harness only sends console commands,
  which need none.
- `targeted.netherNetPatch.*`: NetherNet settings; the benchmark uses RakNet.

## Variants

Both variants add LeviOptimize 0.17.0. Their changes are `nonvanilla` overrides, so every report footnotes them.

### `levioptimize`

LeviOptimize with the config a fresh install generates. Two things are on:

- `fixChunkLeak`: when a player disconnects, LeviOptimize removes that player from the tracking lists of map items.
  The removed entries no longer keep their chunks loaded, which vanilla would. It acts only on a disconnect, so
  `spread100` never triggers it; chaos scenarios with churn do.
- the `timing` command, a diagnostic that does nothing until someone runs it.

A server that upgraded from LeviOptimize 0.13 or older keeps `optMovingBlock` and `optPushEntity.enable` on, because
LeviLamina keeps old config values over new defaults. This variant measures a fresh install.

### `levioptimize-all`

Every LeviOptimize switch on (`servers/levilamina/LeviOptimize.all.json`):

| Option | What changes |
|---|---|
| `optMovingBlock` | block entities moved by a piston reach clients without their contents while moving; saving and the server's simulation are unchanged |
| `optSeenPercent` | an explosion's exposure test is computed once per position and tick and reused; a second explosion at the same spot in the same tick gets the first result even if blocks changed in between |
| `optPushEntity.enable` | turns on the push options: pushes by a zero vector are skipped (`disableVec0Push`) |
| `optPushEntity.maxPushTimes` | a limit, not a switch; stays at `-1` (no limit), as in the legacy runs |

`fixChunkLeak` and the `timing` command stay on as in `levioptimize`.

### Parity of the variants

| Arm | Fidelity | Differs from LeviLamina's row |
|---|---|---|
| `levilamina@26.51.6` | V1 | |
| `levilamina@26.51.6+levioptimize` | V2 | no feature key; after a disconnect, chunks kept loaded by map tracking are released |
| `levilamina@26.51.6+levioptimize-all` | V2 | `block.redstone` altered (`optMovingBlock`), `entity.collision_push` altered (`optPushEntity`), explosion exposure reused within a tick (`optSeenPercent`), plus the above |

None of these keys is exercised by `spread100`, so no arm does less than vanilla on the headline; the difference
shows in the footnotes and the fidelity class. Feature files declare rows per server version, not per variant, so
`run.json` currently records the variants with LeviLamina's own row and fidelity V1. Read their parity from this
table until feature files can declare variant rows.

## Console

| | |
|---|---|
| Line format | `12:00:00.123 IFO [Server] text`; the bracket names the logger (`Server`, `LeviLamina`, `CrashLogger`, a mod's name); every line of multi-line output carries the prefix; `Quit correctly` at exit is bare |
| Levels | `TRC`, `DBG`, `IFO`, `WRN`, `ERR`, `FTL`; PreLoader logs before LeviLamina loads and spells levels out (`INFO`) |
| Ready | `Server started in (6.3s)! For help, type "help" or "?"`, LeviLamina's form of `Server started.` |
| Version banner | `Version: 1.26.51.1(ProtocolVersion 2193) with LeviLamina-26.51.6`; the LeviLamina version must equal the arm's |
| Tick probe, in world, positions, stop, benign errors | as vanilla BDS |

PreLoader's `colorLog` adds colour codes, which the console strips before parsing. Log level counts are not compared
with other servers.

## Process and threads

`bedrock_server_mod.exe` is the measured process. The tick thread is the thread named `MC_SERVER`; Windows has one.
LeviLamina adds about 28 threads: in the legacy runs, 26 numbered `ll_default_thread_pool[N]` threads, one
`ll_default_thread_pool[sch]` and one `logger[N]`. They are reported in the diagnostic groups `levilamina_pool` and
`levilamina_logger`, next to BDS's own groups, and the thread census at the end of idle records their number in every
run.

## Default configuration

LeviLamina's generated configuration runs the benchmark as it is. The only changes are the two uploads above and the
`server.properties` values every BDS-hosted server gets.

## Crashes

Collected on a crash or a nonzero exit: `logs/crash/` (CrashLogger's reports and dumps) and dumps of
`bedrock_server_mod.exe` in `%LOCALAPPDATA%\CrashDumps`.

## Earlier measurements

`published/legacy-0.11.14/results/levilamina/` keeps interim summaries from the legacy harness. They are single runs,
one of them contaminated, made under different conditions; they are not comparable with this matrix.
