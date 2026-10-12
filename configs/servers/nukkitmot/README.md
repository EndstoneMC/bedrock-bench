# Nukkit-MOT (`nukkitmot`)

[Nukkit-MOT](https://github.com/MemoriesOfTime/Nukkit-MOT) (MemoriesOfTime) is a multi-version fork of Nukkit, written
in Java and licensed under LGPL-3.0. It accepts clients from protocol 113 up to 2193 and generates its own terrain.
Its `[features]` rows in [server.toml](server.toml) are fidelity V1, so reports list it under "Reduced fidelity".
Nukkit-MOT publishes rolling builds of its master branch, so a version is the 7-character commit of one Jenkins build.

Adapter: [nukkitmot.py](../../../bedrock_bench/adapters/nukkitmot.py). Helper plugin: [plugin.yml](plugin.yml),
[BenchTick.java](BenchTick.java) and [PreGen.java](PreGen.java).

## Setup

```shell
bbench fetch nukkitmot@9cb0aba
```

Fetch downloads `Nukkit-MOT-SNAPSHOT.jar` of master build #1592 from the project's Jenkins and the Temurin JDK, and
checks each against the sha256 pinned in [server.toml](server.toml) and [runtimes.toml](../runtimes.toml). Neither is
Mojang software, so no licence flag is needed. Jenkins keeps only its last few builds: keep a copy of the jar and pass
its folder with `--artifact-dir`, where it is checked against the same pin under its pinned file name.

The install is built once and holds:

- `server/nukkit-mot.jar`;
- `server/plugins/BenchTick.jar`, the helper compiled with the JDK's `javac --release 21` against the pinned jar. The
  plugin jar is reproducible: sorted entries, fixed timestamps.

The jar ships no config files, so there is no `defaults/` copy.

The cross-server matrix also runs `spread100-pregen`, so build the template first:

```shell
bbench prepare-world nukkitmot@9cb0aba --scenario spread100-pregen
```

Nukkit-MOT builds it through the helper's `pregen` command, the same as Nukkit's.

## How a run starts

```
<jdk>/bin/java -Xms<heap> -Xmx<heap> -Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8 -jar nukkit-mot.jar
```

- The heap is `jvm_heap` from the machine profile, the same for every JVM server, with `-Xms` equal to `-Xmx`.
- Nothing is sent on stdin. `stop` exits with code 0.
- The `java` process is the measured process. Nukkit-MOT runs its game loop on the JVM's `main` thread. Once the
  server is ready, the harness asks `jcmd <pid> Thread.print` for that thread's OS id; when `jcmd` fails, it falls back
  to the thread named `main`.
- Tick probe: `gametime` (helper) prints `Gametime is N`, the server's tick counter. Nukkit-MOT's own `time query`
  prints the day time, which wraps at 24000.
- Positions: `positions` (helper) prints `positions: N`, then one `position: <name> <x> <y> <z> <dimension>` line per
  player.
- The line `Enabling Nukkit-MOT vgit-<commit>` must name the arm's commit.
- The bots need no `-fake-xuid`: an offline player's UUID comes from the name.

## Configuration changes

The values, the defaults and the reasons are in the `[config]` table of [server.toml](server.toml), and every change is
recorded in run.json with its category ([methodology](../../../docs/methodology.md#fairness-and-feature-parity)).
`server.properties` and `nukkit-mot.yml` start empty; Nukkit-MOT adds its own defaults and rewrites both on start.

- harness: the machine profile's port; offline login; `allow-flight=on` (see Caveats); an empty `rcon.password`;
  NetherNet off, because it listens on UDP 19132 whatever `server-port` says; error reports, bStats and the palette
  download off; no update check when an operator joins.
- parity: view and simulation distance, seed, level name, difficulty, game mode, max players, autosave interval and
  player data. `performance-settings.auto-tick-rate` is off: with it, a slow level is ticked only every few server
  ticks while the tick counter keeps advancing.
- rescue, variant `no-watchdog` only: `performance-settings.thread-watchdog: false`.

Held in the adapter:

- `plugins/BenchTick.jar` is installed (harness).
- `-Xms` and `-Xmx` from the machine profile (parity), and UTF-8 console streams (harness).

Left as shipped and recorded in run.json facts: movement mode `server-auth`, async workers `auto`, virtual threads, 4
chunk sends per player per tick. The Nether and the End load at start as separate levels; the chaos dimension action
uses them.

## Variants

- `no-watchdog` (`nukkitmot@9cb0aba+no-watchdog`): the watchdog shuts the server down once a tick takes 60 s. The
  default arm keeps it, so that outcome is a `server_failed` result. Reports footnote the variant. The cross-server
  matrix runs both arms.

## Caveats

- Nukkit-MOT applies no gravity to clients, so bots teleported to the spread height stay there. With
  `allow-flight=off` it kicks them for flying.
- Its movement mode is `server-auth`, but it sends no movement corrections. It sends no locator-bar packets either.
  `corrections_per_bot_s`, `locator_pkts_per_bot_s` and `airborne_frac_steady` check this in every run.
- It generates its own terrain from the seed, which never matches BDS terrain. It sends whole chunk columns in
  `LevelChunk` packets, at most 4 per player per tick, and never lets clients generate terrain.
- At every start it asks the GitHub API for the latest master commit; no setting turns this off. Once master moves
  past the pin, the log carries a `[Nukkit-MOT][Update]` line.
- The RakNet listener exempts the machine's own addresses from the per-address packet limit, so the limit stays at
  its default of 2000 per 10 ms.
- Effects last 1,000,000 s, because there is no `infinite`. Target selectors work, so the spread phase's effects for
  `@a` are sent.
- On stop with many chunks loaded it logs `Pending chunk writes for bench reached 128 (limit 128)` and still exits
  with code 0.

## Crash files

When the server dies, exits nonzero or has to be killed at stop, the harness writes the last log lines to
`crash/server-log-tail.txt` and copies the files written since the run started under `logs/`, plus any JVM fatal error
file (`hs_err_pid*.log`) in the server directory. With error reports off, the watchdog's thread dump goes only to the
console and `logs/server.log`.
