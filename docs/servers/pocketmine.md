# PocketMine-MP (`pocketmine`)

[PocketMine-MP](https://github.com/pmmp/PocketMine-MP) is a Bedrock server written in PHP. It shares no code with
Mojang's server: world generation, simulation and the chunk pipeline are its own, and much of vanilla gameplay is
missing or simpler. Its parity rows (`features/pocketmine.toml`) are fidelity V1 with many features absent or partial,
so reports list it under "Reduced fidelity".

Adapter: `src/bedrock_bench/adapters/pocketmine.py`, built on the behaviour shared by servers that bring their own
runtime in `adapters/standalone.py`. Config values: `servers/pocketmine/config.toml`. Helper plugin:
`servers/pocketmine/plugins/BenchHelper.php`.

## Versions

| Version | Protocol | Minecraft | PHP | Bot |
|---|---|---|---|---|
| 5.44.3 | 1001 | 1.26.30 | PocketMine PHP 8.2, PHP-Binaries build 524 | `legacy1001` |

The 5.44 line speaks protocol 1001 (Minecraft 1.26.30). The main load bot speaks 2193 and cannot join, so PocketMine-MP
runs use `bots/legacy1001`: the same bot behaviour built on gophertunnel v1.57.1 with a login back-port. Every
PocketMine-MP result is flagged `bot_protocol_differs`.

## Setup

```shell
bbench fetch pocketmine@5.44.3
```

Fetch downloads `PocketMine-MP.phar` from the GitHub release and PocketMine's PHP build from
[pmmp/PHP-Binaries](https://github.com/pmmp/PHP-Binaries), and checks both against the sha256 pinned in
`artifacts/pocketmine.toml` and `artifacts/runtimes.toml`. Neither is Mojang software, so no licence flag is needed.
The PHP build (a zip on Windows, a tarball on Linux) is unpacked once into the cache and used read-only. The install
holds:

- `server/PocketMine-MP.phar` and `server/plugins/BenchHelper.php`, copied into each run's fresh server directory;
- `defaults/pocketmine.yml`, the file PocketMine-MP writes on its first start, read out of the phar, so its defaults
  always belong to the pinned release.

## How a run starts

```
<php> PocketMine-MP.phar --no-wizard --disable-ansi        (working directory: the run's server copy)
```

- Windows: `bin/php/php.exe` from the PHP build, which reads the `php.ini` next to it.
- Linux: `bin/php7/bin/php -d extension_dir=<runtime>/bin/php7/lib/php/extensions/no-debug-zts-20220829`. The build's
  `php.ini` loads opcache by name but names no extension directory; PocketMine-MP's installer writes one, and the
  harness passes the same directory on the command line.
- `PHPRC` is removed from the environment, as PocketMine-MP's start scripts do, so no other `php.ini` is read.
- `--no-wizard` skips the setup wizard of a first start; `--disable-ansi` turns off colours and console title updates.

The PHP process is the measured process. PocketMine-MP ticks on PHP's main thread, the first thread of the process,
so the tick thread rule is `first_created`; worker threads (async workers, RakLib) are separate.

## Overrides

`server.properties` starts empty and PocketMine-MP fills in its own defaults for every other key on start.
`pocketmine.yml` starts from the shipped file; a value equal to the shipped one is written and read back after the run,
but is not an override.

| File | Key | Shipped | Benchmark | Category | Reason |
|---|---|---|---|---|---|
| `server.properties` | `server-port` | `19132` | machine profile port (default `19672`) | harness | away from 19132 so a local game cannot collide |
| `server.properties` | `server-portv6` | `19133` | port + 1 | harness | the IPv6 port next to the IPv4 one |
| `server.properties` | `enable-ipv6` | `on` | `off` | harness | the bots use IPv4; with IPv6 on, the server also binds 19133 by default |
| `server.properties` | `xbox-auth` | `on` | `off` | harness | the bots log in offline |
| `server.properties` | `view-distance` | `16` | `10` | parity | same view distance on every server |
| `server.properties` | `max-players` | `20` | `200` | parity | room for every bot, plus rejoins |
| `server.properties` | `level-name` | `world` | `bench` | parity | fixed world folder, checked absent before launch |
| `server.properties` | `level-seed` | empty (random) | `20261010` | parity | same seed on every server |
| `server.properties` | `difficulty` | `2` (normal) | `1` (easy) | parity | same difficulty on every server |
| `server.properties` | `gamemode`, `auto-save` | `SURVIVAL`, `on` | same | none | |
| `pocketmine.yml` | `settings.async-workers` | `auto` | number of server CPUs | parity | `auto` counts every CPU of the machine, not the server's CPU set |
| `pocketmine.yml` | `chunk-ticking.tick-radius` | `3` | `4` | parity | same simulation distance on every server |
| `pocketmine.yml` | `auto-report.enabled` | `true` | `false` | harness | no crash report upload |
| `pocketmine.yml` | `auto-updater.enabled` | `true` | `false` | harness | no update checks during measurement |
| `pocketmine.yml` | `anonymous-statistics.enabled`, `ticks-per.autosave`, `player.save-player-data` | `false`, `6000`, `true` | same | none | |
| `pocketmine.yml` | `memory.main-hard-limit` | `1024` | `0` (off); shipped value in variant `stock` | rescue | see below |
| `plugins/BenchHelper.php` | plugin | absent | installed | harness | tick counter and player positions |
| `plugins/BenchHelper.php` | RakLib per-IP packet limit | 200 packets per RakLib tick | unlimited | harness | see below |
| command line | `--no-wizard`, `--disable-ansi` | absent | set | harness | non-interactive start, plain console |
| environment | `PHPRC` | inherited | unset | harness | PHP reads the `php.ini` of its own build |
| command line (Linux) | `-d extension_dir` | the build machine's path | the bundled extensions | harness | opcache loads, as after PocketMine-MP's installer |

## Default configuration

Started with its shipped configuration, PocketMine-MP fails the workload twice over:

- **Main-thread memory limit (rescue).** With `memory.main-hard-limit: 1024`, PocketMine-MP stops itself about 23 s
  into the 100-bot join, while it populates 100 views that do not overlap. The benchmark turns the limit off and every
  report footnotes the change. The `stock` variant (`pocketmine@5.44.3+stock`) keeps the shipped limit and records the
  failure as a `server_failed` result.
- **Per-IP flood guard (harness).** RakLib blocks an address that sends 200 packets within one RakLib tick, for 300 s.
  Every bot connects from 127.0.0.1, so the whole fleet times out at once and the server idles at 20 TPS with nobody
  on it. BenchHelper lifts the limit one second after start, once the network interface exists.

## Console

| | |
|---|---|
| Line format | `[13:58:00.123] [Server thread/INFO]: text`; replies to console commands are printed as `Command output \| text`, without a level |
| Levels | `DEBUG`; `INFO` and `NOTICE` (info); `WARNING`; `ERROR`; `CRITICAL`, `ALERT`, `EMERGENCY` (fatal) |
| Ready | `Done (3.205s)! For help, type "help" or "?"` |
| Version banner | `This server is running PocketMine-MP version 5.44.3`, which must equal the arm's version |
| Tick probe | `gametime` (BenchHelper) -> `Gametime is N`, the server's tick counter (`Server::getTick()`), which advances once per executed tick. `time query` prints the world time and is not used |
| In world | `<name> joined the game` |
| Positions | `positions` (BenchHelper) -> `positions: N`, then `position: <name> <x> <y> <z> 0` per player |
| Effects | vanilla syntax with `infinite`. There are no target selectors, so the spread phase's effects for `@a` are not sent; each bot got its effects when it was placed |
| Stop | `stop` |

## Chaos

Player commands only: `tp`, `kill`, `kick`, `gamemode`, `effect <player> clear` and `list`. Every other chaos command
is counted as unsupported, with the reason in the adapter: no dimensions, no `summon` and no mob AI, no `loot`, no
entity selectors (cleanup), no `gamerule` (tick speed, waypoints), no weather, no `fill` or `setblock`, no ticking
areas.

## Templates

`bbench prepare-world pocketmine@5.44.3 --scenario spread100-pregen` builds a template with the bot tour of
`bedrock_bench/pregen.py`. PocketMine-MP is not part of the pre-generated cross-server scenario.

## Parity notes

- PocketMine-MP generates its own terrain from the seed; it never matches BDS terrain.
- It sends whole chunk columns in `LevelChunk` packets (no sub-chunk requests) and never lets clients generate
  terrain, so every bot receives the full view disc.
- In the legacy runs it sent no movement corrections and no locator-bar packets, and the bots kept hovering at the
  teleport height. The rows say so, and `corrections_per_bot_s`, `locator_pkts_per_bot_s` and `airborne_frac_steady`
  check it in every run.

## Crashes

On a crash or a nonzero exit, the harness keeps the last log lines and copies files written since the run started under
the server's `crashdumps/` folder.

Docker runs need the `php` image of `docker/` with the same PHP build.
