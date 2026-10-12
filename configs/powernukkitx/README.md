# PowerNukkitX (`powernukkitx`)

[PowerNukkitX](https://github.com/PowerNukkitX/PowerNukkitX) is a Nukkit fork written in Java, licensed under
LGPL-3.0, that reimplements much more of vanilla gameplay: mobs with AI, redstone, fluids, dimensions. It shares no code
with Mojang's server. Its `[features]` rows in [server.toml](server.toml) are fidelity V1, with the systems it has marked
`partial`, so reports list it under "Reduced fidelity". Two versions are pinned: the 3.0.5 release, and
`nightly.86f70bc`, the CI build of the `beta` branch at commit 86f70bc.

Adapter: [powernukkitx.py](../../bedrock_bench/adapters/powernukkitx.py). There is no helper plugin.

## Setup

```shell
bbench fetch powernukkitx@3.0.5
bbench fetch powernukkitx@nightly.86f70bc
```

Fetch downloads the jar from GitHub and the Temurin JDK the version names (21 for 3.0.5, 25 for the nightly), and
checks each against the sha256 pinned in [server.toml](server.toml) and [runtimes.toml](../runtimes.toml). Neither is
Mojang software, so no licence flag is needed. The install is `server/powernukkitx.jar`. The release jar reports itself
as `3.0.5-SNAPSHOT (git-9813b75)`: `commit` in server.toml is the commit it was built from, and the version check reads
`3.0.5` from that banner. The nightly jar reports `nightly-SNAPSHOT (git-86f70bc)`, which reads as `nightly.86f70bc`.

The nightly jar is the asset of the `nightly` pre-release, which upstream replaces with every new build. Once it has
moved, fetch fails on the sha256. Keep a copy named `powernukkitx-nightly-86f70bc.jar` and pass its folder with
`--artifact-dir`.

## How a run starts

```
<jdk>/bin/java -Xms<heap> -Xmx<heap> -Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8 -jar powernukkitx.jar
```

- The heap is `jvm_heap` from the machine profile, the same for every JVM server, with `-Xms` equal to `-Xmx`.
  Without it, PowerNukkitX takes the JVM's default maximum heap, a quarter of physical memory.
- Prepare writes `pnx.yml` with the benchmark's values. PowerNukkitX adds every other key with its built-in default and
  rewrites the file on start.
- Prepare also writes `worlds/<level>/config.json` from [world-config.json](world-config.json), with the scenario seed
  for all three dimensions, because PowerNukkitX reads a level's seed only from there. The level's database
  (`worlds/<level>/db`) must not exist before a fresh run. A template brings the config it was generated with.
- The `java` process is the measured process. The tick thread is the JVM's `main` thread, found with
  `jcmd <pid> Thread.print` once the server is ready, as for Nukkit.
- Tick probe: `time query gametime` prints `Gametime is N`, the server's own tick counter.
- Positions: none. PowerNukkitX has no `querytarget`, so the spread check uses the positions the bots report.
- The bots get `-fake-xuid`: like Nukkit, PowerNukkitX derives a player's UUID from the XUID in the login.

## Configuration changes

The values, PowerNukkitX's built-in defaults and the reasons are in the `[config]` table of
[server.toml](server.toml), and every change is recorded in run.json with its category
([methodology](../../docs/methodology.md#fairness-and-feature-parity)). `pnx.yml` starts empty. Both versions have
the same defaults for the keys written here.

- harness: the machine profile's port; offline login; usage metrics off; the per-IP RakNet packet limit raised,
  because every bot connects from 127.0.0.1 and the default limit is sized for one player per address. The RakNet
  transport is written as well, which is the default.
- parity: view and simulation distance, level name, difficulty, game mode, max players, autosave and its interval, and
  player data. `level-settings.autoTickRate` is off: with it, a slow level is ticked only every few server ticks while
  the tick counter keeps advancing. `level-settings.loadAllLevels` is off, so only the benchmark level loads.

Held in the adapter:

- The seed in `worlds/<level>/config.json` (parity).
- `-Xms` and `-Xmx` from the machine profile (parity), and UTF-8 console streams (harness).

Left at their defaults and recorded in run.json facts: the inbound limits per session (1500 packets per second, 500 per
tick), which apply per player rather than per address; and `levelWorkerThreads: -1`, one worker per available
processor, which with the server pinned is the server's CPU set. The nightly also records `upstream-bandwidth`
(1000 Mbps, the budget of its chunk publisher), `generationQueueSize` (-1, no limit), `leveldb-backend` (java) and
`raknet.globalPacketLimit` (100000 per 10 ms for the whole server).

## Caveats

- It generates its own terrain from the seed, which never matches BDS terrain. It sends whole chunk columns in
  `LevelChunk` packets, with no sub-chunk requests, and never lets clients generate terrain, so every bot receives the
  full view disc. The nightly sends sub-chunks only to clients whose login claims client-side generation support.
- It checks movement (`checkMovement`), but sends no movement corrections and no locator-bar packets, and bots
  teleported to the spread height keep hovering there. The `[features]` rows say so, and `corrections_per_bot_s`,
  `locator_pkts_per_bot_s` and `airborne_frac_steady` check it in every run.
- It loads the chunks it ticks from disk synchronously on its main thread, which shows in the tick-thread CPU of
  `spread100-pregen`.
- It has busy threads besides `main`, so tick-thread CPU covers only part of its work; process CPU covers all of it.
- The nightly runs on JDK 25 and 3.0.5 on JDK 21, so a comparison of the two includes the JDK change. On JDK 25 the
  JVM prints a few `WARNING:` lines at start without a time stamp or level.

## Crash files

When the server dies, exits nonzero or has to be killed at stop, the harness writes the last log lines to
`crash/server-log-tail.txt` and copies the files written since the run started under `logs/`, plus any JVM fatal error
file (`hs_err_pid*.log`) in the server directory.
