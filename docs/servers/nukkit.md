# Nukkit (`nukkit`)

[Nukkit](https://github.com/CloudburstMC/Nukkit) (CloudburstMC) is a Bedrock server written in Java. It shares no
code with Mojang's server and implements a subset of vanilla gameplay with its own world generator. Its parity rows
(`features/nukkit.toml`) are fidelity V1 with several features absent, so reports list it under "Reduced fidelity".

Adapter: `src/bedrock_bench/adapters/nukkit.py`, built on the JVM behaviour in `adapters/standalone.py`. Config
values: `servers/nukkit/config.toml`. Helper plugin sources: `servers/nukkit/plugin/`.

## Versions

| Version | Commit | Protocol | Minecraft | JDK |
|---|---|---|---|---|
| `b3caad6` | `b3caad67d86be34aeca3937e64e8176f6760a2d4` | 2193 | 1.26.50 | Temurin 21.0.12.1 |

Nukkit publishes no releases. A version is the 7-character commit of a snapshot build from the OpenCollab Maven
repository, pinned by its timestamped file name and sha256 in `artifacts/nukkit.toml`. Snapshot repositories prune old
builds: keep a copy of the jar and pass it with `--artifact-dir`, where it is checked against the same pin.

## Setup

```shell
bbench fetch nukkit@b3caad6
```

Fetch downloads the jar and the Temurin JDK (`artifacts/runtimes.toml`), checks both against their sha256 and builds
the install once:

- `server/nukkit.jar`, and `server/plugins/BenchTick.jar`, the helper compiled from `servers/nukkit/plugin/src` with the
  JDK's `javac --release 21` against the pinned jar. The plugin jar is reproducible: sorted entries, fixed timestamps.
  The plugin reaches into Nukkit's RakNet listener by reflection (see below), so it is tied to the pinned commit.
- `defaults/nukkit.yml`, the file Nukkit writes on its first start (`lang/eng/nukkit.yml` in the jar).

The JDK, not a JRE, is pinned because of `javac` and `jcmd`; the `java` binary that runs the server is the same HotSpot
build either way.

## How a run starts

```
<jdk>/bin/java -Xms8G -Xmx8G -Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8 -jar nukkit.jar
```

- The heap comes from the machine profile (`runtime.jvm_heap`), the same for every JVM server, with `-Xms` equal to
  `-Xmx`.
- Nukkit asks for a language on stdin only when `nukkit.yml` is missing. Prepare always writes it, so nothing is sent
  on stdin.
- The `java` process is the measured process. Nukkit runs its game loop on the JVM's `main` thread. Once the server is
  ready, before the idle window, the harness asks `jcmd <pid> Thread.print` for that thread's OS id; when `jcmd`
  fails, it falls back to a thread named `main`.
- Nukkit derives a player's UUID from the XUID in the login, so offline bots without one all get the same UUID and
  each new bot kicks the previous one. The bots get `-fake-xuid`, an allowlisted identity flag.

## Overrides

`server.properties` and `plugins/bStats/config.yml` start empty and Nukkit fills in its own defaults on start.
`nukkit.yml` starts from the shipped file; a value equal to the shipped one is written and read back after the run, but
is not an override.

| File | Key | Shipped | Benchmark | Category | Reason |
|---|---|---|---|---|---|
| `server.properties` | `server-port` | `19132` | machine profile port (default `19672`) | harness | away from 19132 so a local game cannot collide |
| `server.properties` | `xbox-auth` | `on` | `off` | harness | the bots log in offline |
| `server.properties` | `allow-flight` | `off` | `on` | harness | see below |
| `server.properties` | `rcon.password` | random, generated on first start | empty | harness | no generated secret in the run's archived configs; RCON stays off |
| `server.properties` | `max-players` | `20` | `200` | parity | room for every bot, plus rejoins |
| `server.properties` | `level-name` | `world` | `bench` | parity | fixed world folder, checked absent before launch |
| `server.properties` | `level-seed` | empty (random) | `20261010` | parity | same seed on every server |
| `server.properties` | `difficulty` | `2` (normal) | `1` (easy) | parity | same difficulty on every server |
| `server.properties` | `view-distance`, `gamemode`, `auto-save` | `10`, `0`, `on` | same | none | |
| `nukkit.yml` | `level-settings.auto-tick-rate` | `true` | `false` | parity | see below |
| `nukkit.yml` | `chunk-ticking.tick-radius` | `3` | `4` | parity | same simulation distance on every server |
| `nukkit.yml` | `worlds` | `world`, `nether` | none | parity | one overworld level; the shipped list loads a level named `world` next to the benchmark level |
| `nukkit.yml` | `ticks-per.autosave`, `player.save-player-data` | `6000`, `true` | same | none | |
| `nukkit.yml` | `debug.level` (variant `no-watchdog` only) | `1` | `2` | rescue | see below |
| `plugins/bStats/config.yml` | `enabled` | `true` | `false` | harness | no usage statistics upload |
| `plugins/BenchTick.jar` | plugin | absent | installed | harness | tick counter, positions, chaos and pre-generation commands |
| `plugins/BenchTick.jar` | RakNet per-IP packet limit | 120 packets per 10 ms | unlimited | harness | see below |
| command line | `-Xms`, `-Xmx` | 1/64 and 1/4 of physical memory | machine profile heap | parity | same fixed heap on every JVM server |
| command line | `-Dstdout.encoding`, `-Dstderr.encoding` | platform encoding | `UTF-8` | harness | console text is UTF-8 on every platform |

The Nether and End levels still load at start (`allow-nether` and `allow-the-end` stay on, as shipped); without
players they cost little, and the chaos dimension action uses them.

## Default configuration

- **Hovering kick (harness).** Nukkit applies no gravity to clients, so bots teleported to y=300 stay there, and with
  `allow-flight=off` Nukkit kicks them for flying. `allow-flight=on` only stops the kick.
- **Per-IP flood guard (harness).** Nukkit's RakNet listener limits each address to 120 packets per 10 ms and has no
  setting for it. All bots share 127.0.0.1, so the helper raises the limit on the listener's channels one second after
  start.
- **Auto tick rate (parity).** With `auto-tick-rate`, Nukkit ticks a slow level only every few server ticks while the
  server tick counter keeps advancing, which would hide slowness from the tick probe.
- **Watchdog (rescue, variant `no-watchdog`).** Nukkit's watchdog shuts the server down once a tick takes 60 s. In the
  legacy runs the first autosave of a fresh 100-bot world (about 17,600 chunks, saved on the main thread) took longer,
  and the watchdog stopped the server with exit code 0. The default arm keeps the watchdog, so that outcome is a
  `server_failed` result; `nukkit@b3caad6+no-watchdog` turns it off. Nukkit runs the watchdog only below debug level 2,
  so the variant sets `debug.level: 2`, which also prints debug messages. Reports footnote the variant.
- The bots' 180 s spawn timeout matters here: during a 100-bot join, Nukkit can take up to two minutes between a
  bot's login and its spawn.

## Console

| | |
|---|---|
| Line format | `13:58:00 [INFO ] text`, the level padded to five characters; log4j's own start-up warning is `2026-10-10T22:29:18.168Z main WARN text` |
| Levels | `TRACE`, `DEBUG` (debug); `INFO`; `WARN`; `ERROR`; `FATAL` |
| Ready | `Done (3.585s)! For help, type "help" or "?"` |
| Version banner | `This server is running Nukkit version git-b3caad6 "" (API 1.1.0)`; the commit must equal the arm's version |
| Tick probe | `gametime` (helper) -> `Gametime is N`, the server's tick counter (`Server.getTick()`). Nukkit's own `time query` prints an untranslated key without the number |
| In world | `<name> joined the game` |
| Positions | `positions` (helper) -> `positions: N`, then `position: <name> <x> <y> <z> <dimension>` per player |
| Effects | `effect <player> <effect> 1000000 <amplifier> true`: Nukkit has no `infinite`, and 1,000,000 s outlasts any run. There are no target selectors, so the spread phase's effects for `@a` are not sent |
| Stop | `stop` |

## Chaos

Vanilla `tp`, `kill`, `kick`, `gamemode`, `effect <player> clear`, `list` and `weather`; `summon <Entity> <player>` in
Nukkit's syntax (the entity appears at the player, offsets are not used); `kill @e`, which Nukkit applies to every
entity that is not a player; and two helper commands, `dim` (dimension changes) and `fillnear` (block edits around a
player; `grass_block`, `farmland`, `oak_leaves` and `oak_sapling` only). Unsupported: `loot`, ticking areas, the
waypoints game rule, and the tick-speed game rule, because Nukkit's `gamerule` command only runs for a player in game.

## Templates

```shell
bbench prepare-world nukkit@b3caad6 --scenario spread100-pregen
```

Nukkit builds its template through the helper's `pregen` command: it populates every chunk within the template radius
of each grid cell, one grid row at a time with at most 64 populations in flight, unloads (and so saves) each row a few
hundred chunks per tick, then reloads every cell's chunks from storage and reports how many it found. A template is
kept only when every cell reaches 99%. If the watchdog stops the server during this, build with
`--variant no-watchdog`; the template serves both arms.

## Parity notes

- Nukkit generates its own terrain from the seed; it never matches BDS terrain.
- It sends whole chunk columns in `LevelChunk` packets (no sub-chunk requests) and never lets clients generate
  terrain, so every bot receives the full view disc.
- In the legacy runs it sent no movement corrections and no locator-bar packets, and the bots kept hovering. The rows
  say so, and `corrections_per_bot_s`, `locator_pkts_per_bot_s` and `airborne_frac_steady` check it in every run.

## Crashes

On a crash or a nonzero exit, the harness keeps the last log lines and copies files written since the run started under
`logs/`, plus any JVM fatal error file (`hs_err_pid*.log`) in the server directory.

Docker runs need the `jvm` image of `docker/` with the same JDK.
