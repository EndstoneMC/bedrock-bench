# Bedrock Dedicated Server (`bds`)

Mojang's [Bedrock Dedicated Server](https://www.minecraft.net/download/server/bedrock), closed-source C++, run
unmodified from the official zip under the [Minecraft EULA](https://www.minecraft.net/eula). It is the baseline every
BDS-hosted server is compared with and the parity reference: every feature in [`server.toml`](server.toml) is
`vanilla` at fidelity V0. Endstone and LeviLamina take their BDS build from the same pins, so a hosted server and its
vanilla baseline run the same verified binary. Adapter: [`bds.py`](../../bedrock_bench/adapters/bds.py), with the
behaviour every BDS-hosted server shares in [`bdsfamily.py`](../../bedrock_bench/adapters/bdsfamily.py).

## Setup

```shell
bbench fetch bds@1.26.52.3 --accept-eula
```

- Fetch downloads the zip pinned in `[artifacts]` and refuses one whose size or sha256 differs.
- Downloading needs the Minecraft EULA accepted, with `--accept-eula` or `BBENCH_ACCEPT_MINECRAFT_EULA=1`. A copy
  already in the cache, or one in `--artifact-dir DIR` under its original file name, needs neither.
- Mojang takes old builds off its site after a while. Keep the zips and pass them with `--artifact-dir`; they are
  checked against the same pins.
- The zip is unpacked once into the cache with its file modes, so `bedrock_server` stays executable on Linux. The
  install is read-only and its tree hash is checked before every run. Each run starts from a fresh, writable copy.
- On Windows, fetch refuses an install whose deepest file would pass the 259-character path limit. Set
  `BBENCH_CACHE` to a short directory if that happens.

## How a run starts

| Platform | Command, in the run's server copy | Environment |
|---|---|---|
| Windows | `bedrock_server.exe` | unchanged |
| Linux | `./bedrock_server` | `LD_LIBRARY_PATH=.`, as Mojang's instructions say; `LD_PRELOAD` removed |

- `bedrock_server` is the started and the measured process.
- Ready line: `Server started.` The version banner, `Version: 1.26.52.3`, must equal the arm's version.
- Tick probe: `time query gametime`, which replies `Gametime is N`. The counter advances once per executed tick.
- In world: `Player Spawned: <name> xuid: ...`.
- Positions: `querytarget @a`, a JSON array after `Target data:` over several lines. It prints no names, so positions
  are matched to grid cells, not to bots.
- Tick thread: the lowest-id thread named `MC_SERVER`. Windows has one. On Linux, threads started from the tick thread
  inherit its name. Diagnostic thread groups: `Streaming Pool`, `LevelDB`, `IO Thread`, `REST Thread`.

## Configuration changes

The values and reasons are in the `[config]` table of [`server.toml`](server.toml), which every BDS-hosted server
uses. Each value is written into the run's `server.properties` and read back after the run. It is an override only when
it differs from what the pinned build ships, and run.json then records the shipped value next to it. Shipped values
below are those of 1.26.52.3; 1.26.51.1 ships the same.

- `harness`: `online-mode` (shipped `true`), `transport` (`nethernet`), `allow-list` (`true`),
  `enable-lan-visibility` (`true`), `server-port` and `server-portv6` (`19132`, `19133`), `server-name`
  (`Dedicated Server`), `emit-server-telemetry` (absent).
- `parity`: `view-distance` (`32`), `max-players` (`10`), `level-name` (`Bedrock level`), `level-seed` (empty) and
  `client-side-chunk-generation-enabled` (`true`), which the scenario sets.
- Written but equal to the shipped value: `tick-distance` (`4`), `gamemode` (`survival`), `difficulty` (`easy`),
  `server-build-radius-ratio` (`Disabled`).
- `max-threads` (shipped `8`) is left alone and recorded in `facts.max_threads`.

The shipped file cannot run the benchmark. The bots speak RakNet while the server listens on NetherNet,
`online-mode=true` rejects an offline login, and `allow-list=true` refuses every bot. With `transport=raknet` alone,
LAN visibility also binds 19132 and 19133, which collides with a local game or a second server.

## Caveats

- BDS sizes its worker pools from the machine, not from the CPU set it is pinned to, so pool sizes differ between
  machines with the same CPU set. `facts.thread_census_idle` records them in every run.
- During heavy terrain generation only a few of the pool's threads do measurable work at a time. That is how the server
  schedules the work, not a sampling artefact.
- The Linux and Windows builds do not send the same number of locator-bar packets for the same workload. Compare
  `locator_pkts_per_bot_s` within one operating system only.
- With client-side generation on (`spread100-csg`), clients generate distant terrain themselves and the server sends
  far fewer chunks than the full view. BDS does not use it on flat, legacy or void worlds even when the property is on.
  The `csg_matches` gate checks the effective setting, and every ranking scenario turns it off
  ([design decisions](../../docs/methodology.md#design-decisions)).
- BDS prints a `TRANSPORT TYPE ERROR` block at error level whenever RakNet is selected. The adapter treats it as
  benign. Error counts are not comparable across servers.

## Crash files

When the server dies, exits nonzero or never gets ready, the run's `crash/` folder gets the last console lines
(`crash_log_lines` in the scenario's teardown phase, 200 by default) and, on Windows, dumps of `bedrock_server` written to
`%LOCALAPPDATA%\CrashDumps` during the run.
