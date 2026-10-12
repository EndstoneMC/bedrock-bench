# Allay (`allay`)

[Allay](https://github.com/AllayMC/Allay) is a Bedrock server written in Java, licensed under LGPL-3.0. It shares no
code with Mojang's server and implements each system on its own; its only world generators are flat and void. Its
`[features]` rows in [server.toml](server.toml) are fidelity V1, so reports list it under "Reduced fidelity". The
pinned 0.14 line accepts protocols 818 to 1001 (up to Minecraft 1.26.30), which the main load bot cannot join, so its
runs use the [protocol 1001 bot](../../../bots/README.md#protocol-1001) and are flagged `bot_protocol_differs`. Every
scenario that exercises terrain is flagged `terrain_mismatch`.

Adapter: [allay.py](../../../bedrock_bench/adapters/allay.py). Helper plugin: [plugin.json](plugin.json) and
[BenchTick.java](BenchTick.java). World settings: [world-settings.yml](world-settings.yml).

## Setup

```shell
bbench fetch allay@0.14.0
```

Fetch downloads the shaded jar from the GitHub release and the Temurin JDK, and checks each against the sha256 pinned
in [server.toml](server.toml) and [runtimes.toml](../runtimes.toml). Neither is Mojang software, so no licence flag is
needed. The install is built once and holds:

- `server/allay.jar`;
- `server/plugins/BenchTick.jar`, the helper compiled with the JDK's `javac --release 21` against the pinned jar. The
  plugin jar is reproducible: sorted entries, fixed timestamps. The helper starts Allay's console through a class that
  is not part of Allay's API, so it is tied to the pinned release.

Allay writes its own config files on start, so nothing is extracted from the jar. The bots need
`bbench bots build --module legacy1001`. The cross-server matrix runs Allay on `spread100` only.

## How a run starts

```
<jdk>/bin/java -Xms<heap> -Xmx<heap> -Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8 -Dsentry.dsn= -jar allay.jar
```

- The heap is `jvm_heap` from the machine profile, the same for every JVM server, with `-Xms` equal to `-Xmx`.
- Nothing is sent on stdin at start. Allay reads console commands only when the JDK reports a console, which current
  JDKs do not for a piped stdin; the helper then starts Allay's own console reader once the server runs.
- `-Dsentry.dsn=` stops error reports: release builds send logged errors to Sentry, with the DSN in the jar's
  `log4j2.xml` and no setting to turn it off. A DSN given as a system property replaces the jar's, and an empty one
  keeps the SDK from starting.
- The `java` process is the measured process. The default world ticks on its own thread, `World Thread #<level>`;
  the JVM's `main` thread runs the server loop. Once the server is ready, before the idle window, the harness asks
  `jcmd <pid> Thread.print` for the world thread's OS id; when `jcmd` fails, it falls back to the OS thread named
  `World Thread #bench`.
- Ready: `Network interface started at <ip>:<port> (<n> ms)`.
- Tick probe: `gametime` (helper) prints `Gametime is N`, the default world's tick counter, which advances once per
  executed world tick and continues from the time saved with the world.
- Positions: `positions` (helper) prints `positions: N`, then one `position: <name> <x> <y> <z> <dimension>` line per
  player, at feet height.
- The banner `This server is running allay <v> with allay-api <api>` must equal the arm's version. It names no commit;
  the jar's sha256 pins it.
- The bots need no flags. Allay derives an offline player's UUID from the name.

## Configuration changes

The values, the defaults and the reasons are in the `[config]` table of [server.toml](server.toml), and every change is
recorded in run.json with its category ([methodology](../../../docs/methodology.md#fairness-and-feature-parity)).
`server-settings.yml` starts empty; Allay adds its own defaults and rewrites the file on start and stop.

- harness: the machine profile's port, with IPv6 off; offline login; the RakNet per-address limit (120 datagrams per
  10 ms) raised to the largest int, because every bot connects from 127.0.0.1; the dashboard window off; bStats off,
  with a fixed server id so no generated identifier lands in the run's archived configs.
- parity: max players, game mode, difficulty, view and simulation distance, chunk autosave interval and player data.
  Game mode and difficulty apply when the world is created, and every run creates it.

Held in the adapter:

- `worlds/world-settings.yml` is written whole from [world-settings.yml](world-settings.yml), the file Allay writes on
  its first start, with the world named after the level and made the default world (parity). Its flat generator and
  LevelDB storage are Allay's defaults. After the run, the file Allay saved must still name that one world with one
  flat dimension.
- `plugins/BenchTick.jar` is installed, and it starts the console on a piped stdin (harness).
- `-Xms` and `-Xmx` from the machine profile (parity); UTF-8 console streams and the empty Sentry DSN (harness).

## Variants

None.

## Caveats

- No bot has joined Allay yet. The protocol 1001 bot builds only once the `EndstoneMC/gophertunnel` fork tag
  `v1.57.1-bench.1` is published ([Patches](../../../bots/README.md#patches)), so `bbench smoke allay@0.14.0` waits on
  that tag. Until then the join line, the chaos replies with players online and the rows observed by a counter come
  from the source only.
- The world is flat: bedrock, three layers of dirt and grass, with the surface at y 5 and air below y 0. There is no
  seed. The spawn point of a fresh world differs between runs; bots are teleported to their cells after they join.
- It sends whole chunk columns, at most 16 per player per tick, and never lets clients generate terrain. Sub-chunk
  sending exists but is off by default and stays off.
- It sends no movement corrections and no locator-bar packets, and computes no movement for players, so bots
  teleported to the spread height keep hovering there. The `[features]` rows say so, and `corrections_per_bot_s`,
  `locator_pkts_per_bot_s` and `airborne_frac_steady` check it in every run.
- After a slow tick the world loop runs the ticks it fell behind back to back, without skipping any, so the tick
  counter catches up with the wall clock.
- Effects last 1,000,000 s, because Allay's `effect` takes whole seconds and has no `infinite`. Effect ids must be
  plain (`resistance`); namespaced ids are a syntax error.
- Replies to console commands start with `[Server] `, which the dialect removes before matching.
- `kill`, `summon`, `setblock` and `difficulty` refuse the console. Chaos runs kill, summon and setblock as the player
  (`execute as <p> ...`) and cleanup as a random player (`execute as @r run kill @e[type=!player]`).
- Chaos: `tp`, `kill`, `kick`, `gamemode`, `effect <player> clear`, `list`, `weather`, the tick-speed game rule,
  `summon`, `fill`, `setblock` and cleanup. Unsupported: dimension travel, because the world has only an overworld and
  Allay generates no Nether or End terrain; `loot`; ticking areas; the waypoints game rule.

## Crash files

When the server dies, exits nonzero or has to be killed at stop, the harness writes the last log lines to
`crash/server-log-tail.txt` and copies the files written since the run started under `logs/`, plus any JVM fatal error
file (`hs_err_pid*.log`) in the server directory. Allay writes no crash reports of its own.
