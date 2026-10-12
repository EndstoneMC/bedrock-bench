# Fairness

A benchmark that compares different servers is only fair if every server gets the same machine, the same load and the
same chance, and if every difference that remains is visible. This page lists the controls, how each server is
configured to match the scenario, and how config changes are categorised.

## Controls

1. **One harness for every server.** No hand-built reference column. Reports read only `metrics.json`, which the same
   code computes for every server.
2. **Same machine profile and scenario per table**, checked by hash. Placement is verified and recorded as `physical`
   or `vcpu`.
3. **Pinning before the first thread starts.** Windows servers start suspended, get affinity and priority, then resume,
   so JVM and BDS thread pools are sized for the CPUs they actually get. Linux sets affinity before `exec`; Docker uses
   `--cpuset-cpus`.
4. **No contention.** A machine lock, an idle gate before each run and a foreign-load gate on every measured window.
   Contaminated runs are invalid. Foreign load is machine busy time minus the server, bot and harness process trees,
   from per-CPU counters every second, with process attribution every 10 s.
5. **Repeats, rotated order, cooldown** ([METHODOLOGY.md](METHODOLOGY.md#repeats-and-statistics)).
6. **One bot.** One source and one flag set for every server; compatibility flags come from an allowlist; the bot build
   and protocol are recorded, and the PocketMine-MP protocol 1001 build is flagged `bot_protocol_differs`
   ([ADR 0001](adr/0001-one-bot-source.md)).
7. **Equal chunk work.** Every ranking scenario runs client-side generation off, Endstone included. `spread100-csg`
   only reproduces the legacy figures of BDS-hosted servers; `report --publish` refuses a cross-server table from it.
8. **World mode never mixed in one table.** Fresh worlds and templates go in separate tables. Templates are built by
   each server's own pipeline and validated at 99% per-cell coverage; they are never shared between servers.
9. **Every config change is categorised** (below). `rescue` footnotes state the default-config outcome, for example
   "PocketMine-MP crashes about 23 s into the join with its default 1 GiB main-thread memory limit". A `stock` variant
   reproduces the default where useful.
10. **Telemetry, crash upload and auto-update off** where a switch exists. Anything left on is listed in the run's
    facts.
11. **Equal resources.** Equal fixed JVM heap (`-Xms` = `-Xmx`, from the machine profile). PocketMine-MP async workers
    equal the server CPU count. BDS `max-threads` stays at its default and is recorded.
12. **Per-IP flood guards.** All bots connect from 127.0.0.1, which trips per-IP packet limits that real players never
    hit. Each adapter raises them (PocketMine-MP helper plugin, Nukkit helper, PowerNukkitX `packetLimit`) and records
    the change as a `harness` override. LeviLamina's IP frequency limit stays at its default. `dropped == 0` is checked.
13. **Same-build baselines.** A BDS-hosted server is shown with its ratio to vanilla BDS of the exact build it hosts
    (`levilamina@26.51.6` against `bds@1.26.51.1`, `endstone@0.11.14` against `bds@1.26.52.3`). Otherwise the arm is
    flagged `different_bds_build`.
14. **Confounders are disclosed, not hidden.** Allocator (Endstone uses mimalloc on Linux, LeviLamina on Windows), JVM
    heap, known server bugs that change the work (see [PITFALLS.md](PITFALLS.md#server-and-config-parity)), and known
    issues such as the DevTools thread that spins in `endstone@0.11.13` (`known_issue`). They appear as facts and
    footnotes; nothing is subtracted.
15. **Chaos is never ranked.** It is shown at normal tick speed only, with delivered and unsupported action counts.

## Scenario settings and how each server gets them

Scenarios state gameplay settings once (`[server]` in the scenario file); each adapter maps them onto its server's own
config. The exact keys, defaults and reasons are in each server's page under [servers/](servers/) and in every run's
`run.json` (`overrides`).

| Scenario setting | Value (spread100 family) | BDS, Endstone, LeviLamina (`server.properties`) | PocketMine-MP | Nukkit | PowerNukkitX (`pnx.yml`) | Dragonfly |
|---|---|---|---|---|---|---|
| View distance | 10 | `view-distance` | `view-distance` | `view-distance` | `viewDistance` | `Players.MaximumChunkRadius` |
| Simulation distance | 4 | `tick-distance` | `chunk-ticking.tick-radius` | `chunk-ticking.tick-radius` | `chunk-settings.tickRadius` | world tick range, set by the helper |
| Seed | 20261010 | `level-seed` | `level-seed` | `level-seed` | world `config.json`, written before first start | n/a (flat world) |
| Level name | `bench` | `level-name` | `level-name` | `level-name` | level folder | world folder |
| Difficulty | easy | `difficulty` (default easy) | `difficulty` | `difficulty` | `difficulty` | n/a |
| Game mode | survival | `gamemode` (default) | `gamemode` | `gamemode` | `gamemode` | default |
| Max players | 200 | `max-players` | `max-players` | `max-players` | `maxPlayers` | `Players.MaxCount` |
| Autosave | every 6000 ticks | server default | `ticks-per.autosave` | `ticks-per.autosave` | `autosaveDelay` | n/a |
| Player data saved | yes | server default | `player.save-player-data` | `save-player-data` | `savePlayerData` | `Players.SaveData`, `World.SaveData` |
| Client-side generation | off (on in `spread100-csg`) | `client-side-chunk-generation-enabled` | not supported | not supported | not supported | not supported |
| Fixed tick rate | yes | n/a | n/a | `auto-tick-rate: false` | `autoTickRate: false` | n/a |
| Worlds loaded | one | one | one | one | `loadAllLevels: false` | one |
| Heap | machine profile | n/a | n/a | `-Xms` = `-Xmx` | `-Xms` = `-Xmx` | n/a |

Key names follow the pinned versions; where this table and a server page disagree, the server page and the adapter are
authoritative. Difficulty is aligned on easy, the BDS default that the legacy runs used, on every server. Servers
without a terrain generator (Dragonfly runs a flat world) are flagged `terrain_mismatch`.

## Override categories

Every value an adapter writes that differs from the server's default becomes an `Override(category, file, key,
default, value, reason)` in `run.json`.

| Category | Meaning | Examples | Footnote |
|---|---|---|---|
| `harness` | Needed to drive the server at all; does not change the simulation | offline mode, ports, RakNet transport, LAN discovery off, server name, console wrapper, helper plugin, raised per-IP limits, Sentry upload off | no |
| `parity` | Aligns the workload across servers | view and simulation distance, seed, level name, difficulty, max players, client-side generation, fixed heap, fixed tick rate, async workers | no |
| `rescue` | The default config fails the workload; the change lets it run | PocketMine-MP memory limits set to 0, Nukkit watchdog off | yes, with the default outcome |
| `nonvanilla` | Changes gameplay to save work | LeviOptimize options that alter simulation | yes |

Variants select alternative override sets for one server (for example a `stock` variant without a rescue override, or
LeviLamina's `levioptimize-all`). A variant is appended to the arm id with `+`, as in
`levilamina@26.51.6+levioptimize-all`, and appears in every table.

## Equal-work rules

- `spread100` (client-side generation off in the server config and the bot) is the headline for every server,
  Endstone included. Every server builds and sends the full disc, 317 columns at view distance 10.
- `spread100-csg` (client-side generation on where supported) only reproduces the legacy figures of BDS-hosted servers
  in Endstone's BENCHMARK.md. It never appears in a cross-server table.
- `spread100-pregen` separates chunk delivery from generation. Each server loads a template built by its own pipeline;
  template hashes are recorded.
- `csg_effective` (median chunks per player below half the disc) is checked against the scenario. A mismatch makes the
  run invalid for servers that support client-side generation.
- The report puts the work table next to the cost table for every arm, and prints efficiency metrics only next to the
  parity rows they depend on.

## What fairness does not cover

- The bots behave identically and are not a model of real players.
- One machine per table. A server that is better on many cores may lose on few, and the other way round.
- A server's published defaults may already be tuned for a different workload. The harness changes defaults only for
  the reasons above, and records every change.
