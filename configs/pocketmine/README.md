# PocketMine-MP (`pocketmine`)

[PocketMine-MP](https://github.com/pmmp/PocketMine-MP) is a Bedrock server written in PHP, licensed under LGPL-3.0. It
shares no code with Mojang's server: world generation, simulation and the chunk pipeline are its own, and many vanilla
systems are missing or simpler. Its `[features]` rows in [server.toml](server.toml) are fidelity V1, so reports list it
under "Reduced fidelity". The pinned 5.44 line speaks protocol 1001 (Minecraft 1.26.30), which the main load bot cannot
join, so its runs use the [protocol 1001 bot](../../bots/README.md#protocol-1001) and are flagged
`bot_protocol_differs`.

Adapter: [pocketmine.py](../../bedrock_bench/adapters/pocketmine.py). Helper plugin: [BenchHelper.php](BenchHelper.php).

## Setup

```shell
bbench fetch pocketmine@5.44.3
```

Fetch downloads `PocketMine-MP.phar` from the GitHub release and PocketMine's PHP 8.2 build from
[pmmp/PHP-Binaries](https://github.com/pmmp/PHP-Binaries), and checks each against the sha256 pinned in
[server.toml](server.toml) and [runtimes.toml](../runtimes.toml). Neither is Mojang software, so no licence flag is
needed. The PHP build is unpacked once into the cache and used read-only. The install holds the phar,
`plugins/BenchHelper.php`, and the `pocketmine.yml` that the phar ships, read out of the phar so its defaults always
belong to the pinned release. Every run starts from a fresh copy.

## How a run starts

```
<php> PocketMine-MP.phar --no-wizard --disable-ansi
```

- It runs in the run's server copy. On Windows `<php>` is `bin/php/php.exe` from the PHP build. On Linux it is
  `bin/php7/bin/php -d extension_dir=<runtime>/bin/php7/lib/php/extensions/no-debug-zts-20220829`: the build's
  `php.ini` loads opcache by name but names no extension directory, and PocketMine-MP's installer writes the same one.
- `PHPRC` is removed from the environment, so PHP reads only the `php.ini` of its own build.
- `--no-wizard` skips the first-start setup wizard. `--disable-ansi` turns off colours and console title updates.
- The PHP process is the measured process. PocketMine-MP ticks on PHP's main thread, the first thread of the process
  (tick thread rule `first_created`); async workers and RakLib run on their own threads.
- Tick probe: `gametime` (BenchHelper) prints `Gametime is N`, the server's tick counter, which advances once per
  executed tick.
- Positions: `positions` (BenchHelper) prints `positions: N`, then one `position: <name> <x> <y> <z> 0` line per player.
- The banner `This server is running PocketMine-MP version <v>` must equal the arm's version.

## Configuration changes

The values, the shipped defaults and the reasons are in the `[config]` table of [server.toml](server.toml), and every
change is recorded in run.json with its category
([methodology](../../docs/methodology.md#fairness-and-feature-parity)). `server.properties` starts empty and
PocketMine-MP adds its own defaults; `pocketmine.yml` starts from the shipped copy.

- harness: the machine profile's port, with IPv6 off; offline login; crash report upload, the auto-updater and
  anonymous statistics off.
- parity: view and simulation distance, seed, level name, difficulty, game mode, max players, autosave interval and
  player data. `settings.async-workers` is the number of server CPUs, because `auto` counts every CPU of the machine,
  not the server's CPU set.
- rescue: `memory.main-hard-limit` is 0 (off) instead of the shipped 1024 MB, at which the server stops itself about
  23 s into the 100-bot join.

Held in the adapter, all harness:

- `plugins/BenchHelper.php` is installed. 20 ticks after it loads, once the network interface exists, it lifts
  RakLib's per-IP packet limit (200 packets per RakLib tick). Every bot connects from 127.0.0.1, and at the default
  limit RakLib blocks the address for 300 s, so the whole fleet times out at once.
- `--no-wizard`, `--disable-ansi`, `PHPRC` unset, and on Linux `-d extension_dir`.

## Caveats

- It generates its own terrain from the seed, which never matches BDS terrain.
- It sends whole chunk columns in `LevelChunk` packets, with no sub-chunk requests, and never lets clients generate
  terrain, so every bot receives the full view disc.
- It sends no movement corrections and no locator-bar packets, and bots teleported to the spread height keep hovering
  there. The `[features]` rows say so, and `corrections_per_bot_s`, `locator_pkts_per_bot_s` and
  `airborne_frac_steady` check it in every run.
- It has no target selectors, so the spread phase's effects for `@a` are not sent; each bot gets its effects when it is
  placed.
- Worlds are not dimensions, so every position reports dimension 0.

## Crash files

When the server dies, exits nonzero or has to be killed at stop, the harness writes the last log lines to
`crash/server-log-tail.txt` and copies the files written since the run started under the server's `crashdumps/` folder.
