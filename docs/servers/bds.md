# Vanilla Bedrock Dedicated Server (`bds`)

Mojang's Bedrock Dedicated Server, run unmodified from the official zip. It is the reference for every parity row
(`features/bds.toml`: fidelity V0, every feature `vanilla`) and the baseline every BDS-hosted server is compared with.
Endstone and LeviLamina take their BDS build from the same pinned zip, so a BDS-hosted arm and its vanilla baseline
run byte-identical server binaries.

Adapter: `src/bedrock_bench/adapters/bds.py`, with the behaviour shared by all BDS-hosted servers in
`adapters/bdsfamily.py`.

## Versions

| Version | Platforms | Used by |
|---|---|---|
| 1.26.52.3 | Windows, Linux | `endstone@0.11.13` and `endstone@0.11.14` comparison (`matrices/endstone-0.11.14.toml`) |
| 1.26.51.1 | Windows, Linux | same-build baseline for `levilamina@26.51.6` |

URLs, sizes and sha256 are pinned in `artifacts/bds.toml`. A zip that does not match its pin is refused. Mojang
removes old builds from its download site after a while; keep a copy and pass it with `--artifact-dir`, where it is
checked against the same pin.

## Setup

```shell
bbench fetch bds@1.26.52.3 --accept-eula
```

The download is covered by the [Minecraft EULA](https://www.minecraft.net/eula). `--accept-eula` (or
`BBENCH_ACCEPT_MINECRAFT_EULA=1`) is required to download it; a copy already in the cache, or one supplied through
`--artifact-dir DIR` (the zip under its original file name), needs no flag. The zip is unpacked once into the cache
with file permissions kept, so `bedrock_server` stays executable on Linux, made read-only, and verified by its tree
hash before every run. Each run gets a fresh, writable copy with no `worlds/` folder.

On Windows, the deepest file in the zip is 124 characters long. Keep the cache root (`BBENCH_CACHE`, default
`%LOCALAPPDATA%\bedrock-bench`) under about 115 characters, or fetch refuses the install rather than produce a tree the
server cannot read.

Launch:

| Platform | Command | Environment |
|---|---|---|
| Windows | `bedrock_server.exe` in the run's server directory | unchanged |
| Linux | `./bedrock_server` | `LD_LIBRARY_PATH=.` as in Mojang's instructions; `LD_PRELOAD` removed |
| Docker | `./bedrock_server` in the `bds` image (Linux build, mounted, never baked in) | as Linux |

## Overrides

All values come from `servers/bds/properties.toml`. Shipped values are those of the 1.26.52.3 zip; 1.26.51.1 ships
the same ones. A value equal to the shipped one is written and read back after the run, but is not an override.

| Key | Shipped | Benchmark | Category | Reason |
|---|---|---|---|---|
| `server-name` | `Dedicated Server` | `bedrock-bench` | harness | names the benchmark server in logs |
| `online-mode` | `true` | `false` | harness | the bots log in offline with self-signed identities |
| `server-port` | `19132` | machine profile port (default `19672`) | harness | away from 19132 so a local game cannot collide |
| `server-portv6` | `19133` | port + 1 | harness | the IPv6 port next to the IPv4 one |
| `transport` | `nethernet` | `raknet` | harness | the load bot connects over RakNet |
| `enable-lan-visibility` | `true` | `false` | harness | with RakNet, LAN discovery also binds 19132 and 19133 |
| `allow-list` | `true` | `false` | harness | the bots are not on the allow list |
| `emit-server-telemetry` | absent (off) | `false` | harness | telemetry stays off, stated explicitly |
| `view-distance` | `32` | `10` | parity | same view distance on every server |
| `max-players` | `10` | `200` | parity | room for every bot, plus rejoins |
| `level-name` | `Bedrock level` | `bench` | parity | fixed world folder, checked absent before launch |
| `level-seed` | empty (random) | `20261010` | parity | same terrain on every server |
| `client-side-chunk-generation-enabled` | `true` | scenario: `false` in `spread100`, `true` in `spread100-csg` | parity (only when `false`) | see below |
| `tick-distance` | `4` | `4` | none | |
| `gamemode` | `survival` | `survival` | none | |
| `difficulty` | `easy` | `easy` | none | |
| `server-build-radius-ratio` | `Disabled` | `Disabled` | none | |

`max-threads` (shipped `8`) is left alone and recorded in `facts.max_threads`.

## Default configuration

Started with the shipped `server.properties`, the benchmark cannot run: the bots speak RakNet while the server
listens on NetherNet, an offline login is rejected by `online-mode=true`, and every bot is refused by
`allow-list=true`. With `transport=raknet` alone, LAN visibility binds the default ports 19132 and 19133 next to the
configured ones, which collides with a Minecraft client or a second server on the same machine. Each of these is a
harness override, not a performance setting.

## Console

| | |
|---|---|
| Line format | `[2026-10-11 12:54:05:233 INFO] text`; the first line is `NO LOG FILE! - setting up server logging...`; further lines of multi-line output have no prefix |
| Levels | `INFO`, `WARN`, `ERROR` (and `VERBOSE`, `DEBUG`, `FATAL` if printed) |
| Ready | `Server started.` |
| Version banner | `Version: 1.26.52.3`, which must equal the arm's version |
| Tick probe | `time query gametime` -> `Gametime is N`; the counter advances once per executed tick |
| In world | `Player Spawned: <name> xuid: ...` |
| Positions | `querytarget @a` -> `Target data: [ ... ]`, a JSON array over several lines; names are not printed, so positions are matched to grid cells, not to bots |
| Stop | `stop` |
| Benign errors | the `TRANSPORT TYPE ERROR` block BDS prints at error level whenever RakNet is selected |

The game rule replies (`Game rule randomtickspeed has been updated to N`) mark when a chaos tick-speed change took
effect. Commands are written LF-terminated and at least one tick apart.

## Process and threads

`bedrock_server` is the measured process. The tick thread is the thread named `MC_SERVER` with the lowest thread id:
Windows has one; on Linux, threads started from the tick thread inherit its name, and the oldest is the tick thread.
Diagnostic thread groups: `Streaming Pool`, `LevelDB`, `IO Thread`, `REST Thread`.

Things to know when reading the numbers:

- BDS sizes its worker pools from the machine, not from the CPU set it is pinned to. The thread census at the end of
  idle records the pool sizes of every run.
- During heavy terrain generation only a few of the pool's threads are busy at a time. That is how the server
  schedules the work, not a sampling artefact.
- The Linux and Windows builds do not send identical locator-bar packet counts for the same workload; compare
  `locator_pkts_per_bot_s` within one operating system only.
- With client-side chunk generation on (`spread100-csg`), clients generate distant terrain themselves and the server
  sends far fewer chunks than the full view. Servers that do not support it send the whole view, which is why every
  ranking scenario, `spread100` included, turns it off.

## Crashes

On a crash or a nonzero exit, the harness keeps the last log lines and copies files written since the run started
from the server directory and from `%LOCALAPPDATA%\CrashDumps` (Windows; only dumps of `bedrock_server`).
