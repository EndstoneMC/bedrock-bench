# PowerNukkitX (`powernukkitx`)

[PowerNukkitX](https://github.com/PowerNukkitX/PowerNukkitX) is a Nukkit fork that reimplements much more of vanilla
gameplay in Java: mobs with AI, redstone, fluids, dimensions. It shares no code with Mojang's server. Its parity rows
(`features/powernukkitx.toml`) are fidelity V1, with the systems it has marked `partial`, so reports list it under
"Reduced fidelity".

Adapter: `src/bedrock_bench/adapters/powernukkitx.py`, built on the JVM behaviour in `adapters/standalone.py`. Config
values: `servers/powernukkitx/config.toml`; world seed template: `servers/powernukkitx/world-config.json`.

## Versions

| Version | Commit | Protocol | Minecraft | JDK |
|---|---|---|---|---|
| 3.0.5 | `9813b75a973aa93d3c29c49c85742a0c1beee3de` | 2193 | 1.26.50 | Temurin 21.0.12.1 |

The 3.0.5 release jar reports itself as `3.0.5-SNAPSHOT (git-9813b75)`; the commit is the one GitHub tags as 3.0.5,
and the version check reads `3.0.5` from that banner.

## Setup

```shell
bbench fetch powernukkitx@3.0.5
```

Fetch downloads the release jar from GitHub and the Temurin JDK (`artifacts/runtimes.toml`) and checks both against
their pinned sha256. The install is `server/powernukkitx.jar`; there is no helper plugin.

## How a run starts

```
<jdk>/bin/java -Xms8G -Xmx8G -Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8 -jar powernukkitx.jar
```

- The heap comes from the machine profile, the same for every JVM server, with `-Xms` equal to `-Xmx`. PowerNukkitX
  defaults to the JVM's default maximum heap, a quarter of physical memory.
- Prepare writes `pnx.yml` with the benchmark's values; PowerNukkitX adds every other key with its built-in default and
  rewrites the file on start. With `pnx.yml` present it shows no setup wizard.
- Prepare also writes `worlds/<level>/config.json` with the scenario seed for all three dimensions, because
  PowerNukkitX reads a level's seed only from there. The level's database (`worlds/<level>/db`) must not exist before a
  fresh run.
- The `java` process is the measured process; the tick thread is the JVM's `main` thread, found with `jcmd` as for
  Nukkit.
- Like Nukkit, PowerNukkitX derives a player's UUID from the XUID, so the bots get `-fake-xuid`.

## Overrides

`pnx.yml` starts empty; `default` below is PowerNukkitX's built-in value, which is what it writes on its first start.

| File | Key | Default | Benchmark | Category | Reason |
|---|---|---|---|---|---|
| `pnx.yml` | `settings.port` | `19132` | machine profile port (default `19672`) | harness | away from 19132 so a local game cannot collide |
| `pnx.yml` | `settings.xboxAuth` | `true` | `false` | harness | the bots log in offline |
| `pnx.yml` | `network-settings.raknet.packetLimit` | `8000` | `1000000` | harness | every bot connects from 127.0.0.1; the per-IP limit is sized for one player per address |
| `pnx.yml` | `misc-settings.enableMetrics` | `true` | `false` | harness | no usage statistics upload |
| `pnx.yml` | `network-settings.transport` | `raknet` | same | none | the bots connect over RakNet |
| `pnx.yml` | `settings.maxPlayers` | `20` | `200` | parity | room for every bot, plus rejoins |
| `pnx.yml` | `settings.defaultLevelName` | `world` | `bench` | parity | fixed world folder |
| `pnx.yml` | `gameplay-settings.viewDistance` | `8` | `10` | parity | same view distance on every server |
| `pnx.yml` | `level-settings.autoTickRate` | `true` | `false` | parity | a slow level would be ticked less often without the tick counter showing it |
| `pnx.yml` | `level-settings.loadAllLevels` | `true` | `false` | parity | only the benchmark level is loaded |
| `pnx.yml` | `gameplay-settings.difficulty`, `gameplay-settings.gamemode`, `chunk-settings.tickRadius`, `settings.autoSave`, `settings.autosaveDelay`, `player-settings.savePlayerData` | `1`, `0`, `4`, `true`, `6000`, `true` | same | none | |
| `worlds/bench/config.json` | `generators.*.seed` | random | `20261010` | parity | same seed on every server |
| command line | `-Xms`, `-Xmx` | 1/64 and 1/4 of physical memory | machine profile heap | parity | same fixed heap on every JVM server |
| command line | `-Dstdout.encoding`, `-Dstderr.encoding` | platform encoding | `UTF-8` | harness | console text is UTF-8 on every platform |

PowerNukkitX also limits inbound packets per session (1500 per second, 500 per tick). Those limits apply per player,
which the bots stay within, so they keep their defaults and are recorded in the run's facts. `levelWorkerThreads: -1`
(one worker per available processor) also stays; with the server pinned, that is the server's CPU set.

## Default configuration

With its defaults PowerNukkitX ticks slow levels less often (`autoTickRate`) and limits every address to 8000 packets,
which 100 bots on 127.0.0.1 exceed. Its default heap is a quarter of physical memory, so an unpinned heap would differ
between machines. Each of these is an override above; none needs a rescue.

## Console

| | |
|---|---|
| Line format | `13:58:00 [main] [INFO] text`, with the thread name; log4j's own start-up warning is `2026-10-10T22:29:18.175Z main WARN text` |
| Levels | `TRACE`, `DEBUG` (debug); `INFO`; `WARN`; `ERROR`; `FATAL` |
| Ready | `Done (5.406s)! For help, type "help" or "?"` |
| Version banner | `This server is running PowerNukkitX version 3.0.5-SNAPSHOT (git-9813b75) (API 3.0.5)` |
| Tick probe | `time query gametime` -> `Gametime is N`; `bbench smoke` checks that it advances once per executed tick |
| In world | `<name> joined the game` |
| Positions | none: PowerNukkitX has no `querytarget`, so the spread check uses the positions the bots report |
| Effects | vanilla syntax with `infinite` and `@a` |
| Stop | `stop` |

## Chaos

The vanilla command set, rendered as for BDS: teleports, dimension changes through `execute in`, effects, `kill`,
`kick`, `gamemode`, `summon`, `list`, the `randomtickspeed` game rule, weather, `fill`, `setblock` and ticking areas.
Unsupported: `loot` (no command), the `playerwaypoints` game rule (does not exist), and cleanup, because
`kill @e[type=!player]` also kills players on PowerNukkitX.

## Templates

```shell
bbench prepare-world powernukkitx@3.0.5 --scenario spread100-pregen
```

PowerNukkitX generates chunks only for areas a player has loaded, so it cannot pre-generate from a plugin. Its
template is built with the bot tour of `bedrock_bench/pregen.py`: ten bots visit the grid cells in rounds with the view
distance raised to the template radius, and the server generates and saves what they see through its normal pipeline.
A cell's coverage is the share of the view disc the bot on it received; the template is kept only when every cell
reaches 99%. At the legacy rate of about 48 chunks per second, the tour takes roughly a quarter of an hour.

## Parity notes

- PowerNukkitX generates its own terrain from the seed; it never matches BDS terrain.
- It sends whole chunk columns in `LevelChunk` packets (no sub-chunk requests) and never lets clients generate
  terrain, so every bot receives the full view disc.
- It checks movement (`checkMovement`), but in the legacy runs it sent no movement corrections and no locator-bar
  packets, and the bots kept hovering. The rows say so, and `corrections_per_bot_s`, `locator_pkts_per_bot_s` and
  `airborne_frac_steady` check it in every run.
- On a pre-generated world in the legacy runs, its main thread spent most of its time loading ticked chunks from
  disk, which shows in the tick-thread CPU of `spread100-pregen`.

## Crashes

On a crash or a nonzero exit, the harness keeps the last log lines and copies files written since the run started under
`logs/`, plus any JVM fatal error file (`hs_err_pid*.log`) in the server directory.

Docker runs need the `jvm` image of `docker/` with the same JDK.
