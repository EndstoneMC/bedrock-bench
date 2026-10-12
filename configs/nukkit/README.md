# Nukkit (`nukkit`)

[Nukkit](https://github.com/CloudburstMC/Nukkit) (CloudburstMC) is a Bedrock server written in Java, licensed under
GPL-3.0. It shares no code with Mojang's server and implements a subset of vanilla gameplay with its own world
generator. Its `[features]` rows in [server.toml](server.toml) are fidelity V1, so reports list it under "Reduced
fidelity". Nukkit publishes no releases, so a version is the 7-character commit of a snapshot build.

Adapter: [nukkit.py](../../bedrock_bench/adapters/nukkit.py). Helper plugin: [plugin.yml](plugin.yml),
[BenchTick.java](BenchTick.java) and [PreGen.java](PreGen.java).

## Setup

```shell
bbench fetch nukkit@b3caad6
```

Fetch downloads the snapshot jar from the OpenCollab Maven repository and the Temurin JDK, and checks each against the
sha256 pinned in [server.toml](server.toml) and [runtimes.toml](../runtimes.toml). Neither is Mojang software, so no
licence flag is needed. Snapshot repositories prune old builds: keep a copy of the jar and pass its folder with
`--artifact-dir`, where it is checked against the same pin under its pinned file name.

The install is built once and holds:

- `server/nukkit.jar`;
- `server/plugins/BenchTick.jar`, the helper compiled with the JDK's `javac --release 21` against the pinned jar. The
  plugin jar is reproducible: sorted entries, fixed timestamps. The helper reaches into Nukkit's RakNet listener by
  reflection, so it is tied to the pinned commit;
- `defaults/nukkit.yml`, the file Nukkit writes on its first start (`lang/eng/nukkit.yml` in the jar).

The JDK, not a JRE, is pinned because of `javac` and `jcmd`.

The cross-server matrix also runs `spread100-pregen`, so build the template first:

```shell
bbench prepare-world nukkit@b3caad6 --scenario spread100-pregen
```

Nukkit builds it through the helper's `pregen` command: it populates every chunk within the template radius of each
grid cell, one grid row at a time with at most 64 populations in flight, unloads (and so saves) each row a few hundred
chunks per tick, then reloads every cell's chunks from storage and reports how many it found. The template is kept only
when every cell reaches the scenario's minimum coverage. If the watchdog stops the server during this, build with
`--variant no-watchdog`; the template serves both arms.

## How a run starts

```
<jdk>/bin/java -Xms<heap> -Xmx<heap> -Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8 -jar nukkit.jar
```

- The heap is `jvm_heap` from the machine profile, the same for every JVM server, with `-Xms` equal to `-Xmx`.
- Nothing is sent on stdin. Nukkit asks for a language only when `nukkit.yml` is missing, and prepare always writes it.
- The `java` process is the measured process. Nukkit runs its game loop on the JVM's `main` thread. Once the server is
  ready, before the idle window, the harness asks `jcmd <pid> Thread.print` for that thread's OS id; when `jcmd` fails,
  it falls back to the thread named `main`.
- Tick probe: `gametime` (helper) prints `Gametime is N`, the server's tick counter. Nukkit's own `time query` prints no
  number.
- Positions: `positions` (helper) prints `positions: N`, then one `position: <name> <x> <y> <z> <dimension>` line per
  player.
- The banner `This server is running Nukkit version git-<commit>` must name the arm's commit.
- The bots get `-fake-xuid`, the one allowlisted identity flag. Nukkit derives a player's UUID from the XUID in the
  login, so offline bots without one would share a UUID and each new bot would kick the previous one.

## Configuration changes

The values, the defaults and the reasons are in the `[config]` table of [server.toml](server.toml), and every change is
recorded in run.json with its category ([methodology](../../docs/methodology.md#fairness-and-feature-parity)).
`nukkit.yml` starts from the shipped copy; `server.properties` and `plugins/bStats/config.yml` start empty and Nukkit
adds its own defaults.

- harness: the machine profile's port; offline login; `allow-flight=on` (see Caveats); an empty `rcon.password`, so no
  generated secret lands in the run's archived configs (RCON stays off); bStats off.
- parity: view and simulation distance, seed, level name, difficulty, game mode, max players, autosave interval and
  player data. `level-settings.auto-tick-rate` is off: with it, Nukkit ticks a slow level only every few server ticks
  while the tick counter keeps advancing, which would hide slowness from the probe. `worlds` is emptied, because the
  shipped list loads a level named `world` next to the benchmark level.
- rescue, variant `no-watchdog` only: `debug.level: 2`.

Held in the adapter:

- `plugins/BenchTick.jar` is installed (harness). 20 ticks after it loads, it lifts the per-IP limit of Nukkit's RakNet
  listener (120 packets per 10 ms), which has no setting. Every bot connects from 127.0.0.1.
- `-Xms` and `-Xmx` from the machine profile (parity), and UTF-8 console streams (harness).

## Variants

- `no-watchdog` (`nukkit@b3caad6+no-watchdog`): Nukkit's watchdog shuts the server down once a tick takes 60 s. In the
  legacy runs the first autosave of a fresh 100-bot world, which Nukkit runs on the main thread, took longer, and the
  watchdog stopped the server with exit code 0. The default arm keeps the watchdog, so that outcome is a `server_failed`
  result. Nukkit runs the watchdog only below debug level 2, so the variant sets `debug.level: 2`, which also prints
  debug messages. Reports footnote it. The cross-server matrix runs both arms.

## Caveats

- Nukkit applies no gravity to clients, so bots teleported to the spread height stay there. With `allow-flight=off` it
  kicks them for flying; `allow-flight=on` only stops the kick.
- It sends no movement corrections and no locator-bar packets. The `[features]` rows say so, and
  `corrections_per_bot_s`, `locator_pkts_per_bot_s` and `airborne_frac_steady` check it in every run.
- During a 100-bot join it can take up to two minutes between a bot's login and its spawn, which the scenario's spawn
  timeout allows for.
- It generates its own terrain from the seed, which never matches BDS terrain. It sends whole chunk columns in
  `LevelChunk` packets, with no sub-chunk requests, and never lets clients generate terrain, so every bot receives the
  full view disc.
- Effects last 1,000,000 s, because Nukkit has no `infinite`. It has no target selectors, so the spread phase's effects
  for `@a` are not sent; each bot gets its effects when it is placed.

## Crash files

When the server dies, exits nonzero or has to be killed at stop, the harness writes the last log lines to
`crash/server-log-tail.txt` and copies the files written since the run started under `logs/`, plus any JVM fatal error
file (`hs_err_pid*.log`) in the server directory.
