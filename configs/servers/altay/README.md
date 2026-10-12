# Altay (`altay`)

[Altay](https://github.com/altayofficial/Altay) is a fork of PocketMine-MP 5, written in PHP and licensed under
LGPL-3.0. It is not TuranicTeam's archived Altay. It keeps PocketMine-MP's gameplay code and console and follows the
current Bedrock protocol: 5.44.7 speaks protocol 2193 (Minecraft 1.26.50), so its runs use the main load bot. Like
PocketMine-MP it shares no code with Mojang's server. Its `[features]` rows in [server.toml](server.toml) are fidelity
V1, so reports list it under "Reduced fidelity". Its version numbers follow PocketMine-MP's, so reports tell the two
apart by the arm tag, `altay@<version>`.

Adapter: [altay.py](../../../bedrock_bench/adapters/altay.py), which extends
[pocketmine.py](../../../bedrock_bench/adapters/pocketmine.py). Helper plugin: [BenchHelper.php](BenchHelper.php).

## Setup

```shell
bbench fetch altay@5.44.7
```

Fetch downloads `Altay.phar` from the GitHub release and PocketMine's PHP 8.2 build from
[pmmp/PHP-Binaries](https://github.com/pmmp/PHP-Binaries), the one PocketMine-MP uses, and checks each against the
sha256 pinned in [server.toml](server.toml) and [runtimes.toml](../runtimes.toml). Neither is Mojang software, so no
licence flag is needed. The release names Altay's own PHP 8.1 binaries; Altay runs on PocketMine's 8.2 build. The
install holds the phar, `plugins/BenchHelper.php`, and the `pocketmine.yml` that the phar ships, read out of the phar
so its defaults always belong to the pinned release. Every run starts from a fresh copy.

The cross-server matrix runs Altay on `spread100` only. `bbench prepare-world altay@5.44.7 --scenario
spread100-pregen` can still build a template for it with the bot tour.

## How a run starts

```
<php> Altay.phar --no-wizard --disable-ansi
```

- It runs in the run's server copy, with PocketMine-MP's PHP command line: `bin/php/php.exe` on Windows, and
  `bin/php7/bin/php -d extension_dir=<runtime>/bin/php7/lib/php/extensions/no-debug-zts-20220829` on Linux.
- `PHPRC` is removed from the environment, so PHP reads only the `php.ini` of its own build.
- `--no-wizard` skips the first-start setup wizard. `--disable-ansi` turns off colours and console title updates.
- The PHP process is the measured process. Altay ticks on PHP's main thread, the first thread of the process (tick
  thread rule `first_created`); async workers and the RakNet transport run on their own threads, which have no names.
- Tick probe: `gametime` (BenchHelper) prints `Gametime is N`, the server's tick counter, which advances once per
  executed tick.
- Positions: `positions` (BenchHelper) prints `positions: N`, then one `position: <name> <x> <y> <z> 0` line per player.
- The banner `This server is running Altay version <v>` must equal the arm's version.

## Configuration changes

The values, the shipped defaults and the reasons are in the `[config]` table of [server.toml](server.toml), and every
change is recorded in run.json with its category
([methodology](../../../docs/methodology.md#fairness-and-feature-parity)). `server.properties` starts empty and Altay
adds its own defaults; `pocketmine.yml` starts from the shipped copy. The changes are PocketMine-MP's, plus the
transport.

- harness: the machine profile's port, with IPv6 off; offline login; `network.transport` written as `raknet`, the
  shipped value; crash report upload, the auto-updater and anonymous statistics off. Altay ships all three on.
- parity: view and simulation distance, seed, level name, difficulty, game mode, max players, autosave interval and
  player data. `settings.async-workers` is the number of server CPUs, because `auto` counts every CPU of the machine,
  not the server's CPU set.
- rescue: `memory.main-hard-limit` is 0 (off) instead of the shipped 1024 MB. See Variants.

Held in the adapter, all harness:

- `plugins/BenchHelper.php` is installed. 20 ticks after it loads, once the network interface exists, it raises the
  RakNet transport's per-IP packet limit from 200 packets per transport tick (10 ms) to 2147483647. Every bot connects
  from 127.0.0.1, and at the default limit the transport blocks the address for 300 s.
- `--no-wizard`, `--disable-ansi`, `PHPRC` unset, and on Linux `-d extension_dir`.

## Variants

- `stock` (`altay@5.44.7+stock`) keeps the shipped memory limits, including the 1024 MB hard limit of the main thread,
  the limit at which PocketMine-MP stops itself about 23 s into the 100-bot join. The default arm turns the limit off,
  and reports footnote that.

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
- Every start logs four `WARNING` lines saying the RakNet transport is deprecated. The bots use RakNet.
- Every join logs three `WARNING` lines about properties of the bot's self-signed login (`ipt`, `tid`, `PlayFabId`).
  The join succeeds.
- The packet limit reaches the transport thread as a signed 32-bit integer. PocketMine-MP's helper passes
  `PHP_INT_MAX`, which wraps to -1 on Altay and blocks every address on its second packet within a tick, so Altay has
  its own copy of the helper.

## Crash files

When the server dies, exits nonzero or has to be killed at stop, the harness writes the last log lines to
`crash/server-log-tail.txt` and copies the files written since the run started under the server's `crashdumps/` folder.
After an uncaught error Altay writes `crashdumps/<date>_UTC.log` and waits until two minutes after start before it
exits with code 1. A plugin that fails to enable stops the server before the ready line.
