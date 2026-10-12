# Dragonfly (`dragonfly`)

[Dragonfly](https://github.com/df-mc/dragonfly) is a Bedrock server written in Go. It shares no code with Mojang's
server: terrain, movement, entities and block behaviour are its own, and much of vanilla is absent
(`features/dragonfly.toml`, fidelity V1). It is MIT-licensed and needs no EULA, which is why it is the one real server
that hosted CI runs (`.github/workflows/e2e-dragonfly.yml`).

Dragonfly is a library with an example server and no binary releases. The harness builds `dfbench`
(`servers/dragonfly/`): Dragonfly's example server plus a console on standard input. Adapter:
`src/bedrock_bench/adapters/dragonfly.py`.

## Versions

| Version | Module version | Platforms | Used by |
|---|---|---|---|
| 1793369 | `v0.11.6-0.20260920163020-1793369709ce` (commit `1793369709ce`, 2026-09-20) | Windows, Linux, Docker | cross-server matrix, CI smoke |

A build from a commit is tagged with the commit's first seven characters; a release would be tagged with its version
(`dragonfly@0.11.5`). `artifacts/dragonfly.toml` pins, per version, the module version, its `go.sum` hash, and the
URL, sha256 and size of the module zip on the Go module proxy.

## Setup

```shell
bbench fetch dragonfly@1793369
```

Fetch needs the Go toolchain (1.26 or later; `servers/dragonfly/go.mod` names `toolchain go1.26.3`, which Go downloads
if the installed one is older) and network access to the Go module proxy. It:

1. checks that `servers/dragonfly/go.mod` requires the pinned module version, that `go.sum` records the pinned hash,
   and that `go.mod` has no `replace` directive;
2. downloads the module with `go mod download` and checks its `go.sum` hash and the sha256 and size of the zip against
   the pin (Go itself verifies every module against `go.sum`);
3. builds dfbench with the same flags as the release builds (`CGO_ENABLED=0`, `GOWORK=off`, `-mod=readonly`,
   `GOAMD64=v1`, `-trimpath -buildvcs=false -ldflags "-s -w -buildid= ..."`), so the same source and Go version give
   the same binary;
4. runs `dfbench -version`, which must report the pinned Dragonfly version and the bots' protocol (2193), and
   `dfbench -defaults`, which records the build's default configuration next to the binary.

The install is cached, made read-only and keyed by the Dragonfly version, the target, the hash of dfbench's sources,
the Go version and the harness version, so editing dfbench or upgrading Go rebuilds it. Each run gets a fresh copy with
no world folder. Under the Docker runtime the binary is built for Linux, and a host build of the same source supplies
`-version` and `-defaults`.

To measure a dfbench built elsewhere, for example against a Dragonfly fork, pass it as the arm's artifact:
`bbench run-one --server dragonfly@devel --artifact path/to/dfbench ...`. A dfbench whose Dragonfly comes from a
`replace` directive or a Go workspace reports Dragonfly version `devel`, so its banner only matches an arm tagged
`devel`, and the run is marked `unpublished_artifact`.

Launch:

| Platform | Command | Environment |
|---|---|---|
| Windows, Linux | `dfbench -config config.toml` in the run's server directory | `GOMAXPROCS`, `GOGC`, `GOMEMLIMIT` and `GODEBUG` removed |
| Docker | `./dfbench -config config.toml` in the `dragonfly` image | as above |

## dfbench

dfbench starts the server from `config.toml` the way Dragonfly's `main.go` does, and adds only what the harness needs:

- a console on standard input (commands below);
- a version banner, `Dragonfly version <version> (module ..., Minecraft ..., protocol ..., go..., GOMAXPROCS n)`;
- `Player spawned: <name>` once a player is in the world (Dragonfly prints nothing when a player joins, and a player
  that has connected is not yet in the world, so earlier commands would miss it);
- a `[Bench]` table in `config.toml` for settings Dragonfly only exposes through its Go API;
- `dfbench.json` in the server directory at startup: build information, `GOMAXPROCS`, the CPU count Go sees, and the
  world settings in effect (difficulty and game mode read back from the world). The adapter records it in
  `run.json` (`server.facts`).

With no `[Bench]` table and no flags, dfbench behaves like the example server, including writing Dragonfly's default
`config.toml` when the file is missing. Commands that touch the world run as tasks on the world or player that owns the
data, so a slow tick delays the reply, never the console. Replies are logged at info level; a command that fails says
why at error level, which `bbench smoke` treats as a failed command.

| Command | Reply |
|---|---|
| `gametime` | `Gametime is N`: the overworld's tick counter (below) |
| `list` | `There are N/M players online:`, then the names |
| `positions` | `Positions: N`, then `Position: <dimension id> <x> <y> <z> <name>` per player |
| `tp <target> <x> <y> <z>` | teleport within the target's dimension |
| `tpdim <player> <overworld\|nether\|the_end> <x> <y> <z>` | teleport into a dimension |
| `effect <target> <effect> [seconds\|infinite] [amplifier] [hideParticles]` | as the vanilla command; `resistance`, `fire_resistance`, `water_breathing`, `saturation` |
| `effect <target> clear` | remove every effect |
| `kill <target>` | void damage; a player already dead is reported, not an error |
| `kick <target> [reason]` | disconnect |
| `gamemode <survival\|creative\|adventure\|spectator> <target>` | |
| `weather <clear\|rain\|thunder> [seconds]` | rain and thunder last 300 s by default |
| `fillnear <player> <dx1> <dy1> <dz1> <dx2> <dy2> <dz2> <block>` | fill a box given by offsets from the player, at most 32768 blocks; `air`, `dirt`, `farmland`, `grass_block`, `oak_leaves`, `oak_sapling`, `stone` |
| `help`, `stop` | |

A target is a player name (any case) or `@a`. Names with spaces go in double quotes. End of input stops the server, so
a harness that goes away takes the server with it.

The `[Bench]` table:

| Key | Default (example server) | Meaning |
|---|---|---|
| `LogLevel` | `debug` | lowest level logged: `debug`, `info`, `warn`, `error` |
| `IdleTicking` | `false` | keep the overworld ticking while nobody is in it (see Tick probe) |
| `SaveIntervalSeconds` | `600` | autosave interval; 0 keeps Dragonfly's default, negative turns autosave off |
| `TickRange` | from `level.dat` (6 in a new world) | chunks around each player whose blocks and entities tick |
| `Difficulty` | from `level.dat` (`normal` in a new world) | `peaceful`, `easy`, `normal`, `hard` |
| `GameMode` | from `level.dat` (`creative` in a new world) | game mode of players joining for the first time |

## Overrides

Values come from `servers/dragonfly/config.toml`; only those keys are written into the run's `config.toml`, and every
other key keeps the build's default as printed by `dfbench -defaults`. A value equal to the default is written and read
back after the run, but is not an override.

| Key | Default | Benchmark | Category | Reason |
|---|---|---|---|---|
| `Network.Address` | `:19132` | `127.0.0.1:` + machine profile port (default 19672) | harness | the bots connect over loopback; a loopback-only listener never triggers a firewall prompt for the newly built binary |
| `Server.Name` | `Dragonfly Server` | `bedrock-bench` | harness | names the benchmark server in the server list |
| `Server.AuthEnabled` | `true` | `false` | harness | the bots log in offline with self-signed identities |
| `Bench.LogLevel` | `debug` | `info` | harness | at debug level Dragonfly logs every packet it does not handle |
| `Bench.IdleTicking` | `false` | `true` | harness | keeps the tick counter running during idle (see Tick probe) |
| `Players.MaximumChunkRadius` | `32` | `10` | parity | the same view distance on every server |
| `Players.MaxCount` | `0` (no limit) | `200` | parity | room for every bot, plus rejoins |
| `World.Folder` | `world` | `bench` | parity | a fixed world folder, checked absent before launch |
| `Bench.TickRange` | `6` | `4` | parity | the same simulation distance on every server |
| `Bench.Difficulty` | `normal` | `easy` | parity | the same difficulty on every server |
| `Bench.GameMode` | `creative` | `survival` | parity | a new Dragonfly world puts players in creative |
| `Bench.SaveIntervalSeconds` | `600` | `300` | parity | the same autosave interval as BDS (6000 ticks) |
| `Players.SaveData` | `true` | `true` | none | player data saved as on every server |
| `World.SaveData` | `true` | `true` | none | the world saved to disk as on every server |

Left at their defaults and worth knowing: the RakNet transport (`Network.Transport`), join and quit messages (printed
to every player and to the console), one chunk-loading worker per world, and Dragonfly's random tick speed of 3.
Nothing here is `rescue` or `nonvanilla`.

## Default configuration

Run with Dragonfly's defaults, the benchmark does not work or does not compare:

- `Server.AuthEnabled = true` rejects the offline bots, and port 19132 collides with a local game.
- Players join a new world in creative mode, where they take no damage and may fly.
- The view distance cap is 32 chunks instead of 10, so each bot would load far more chunks than on other servers.
- At debug level the console prints a line for every packet the server does not handle, for every bot.
- With nobody in the world, the overworld does not tick and its tick counter stops, so idle TPS reads 0 and the
  `probe_monotonic` gate (counter must advance during idle) fails. See Tick probe.

## Console

| | |
|---|---|
| Line format | `2026/01/01 12:00:00.105301 INFO text`: Go's log package with microseconds and the slog level |
| Levels | `DEBUG`, `INFO`, `WARN`, `ERROR` (a custom slog level prints as e.g. `ERROR+2` and maps to its base level) |
| Unprefixed lines | chat broadcasts such as `a00 joined the game`, and Go panics |
| Ready | `Server started.` (dfbench's line, after the listener is up; Dragonfly's own `Dragonfly server started.` comes earlier) |
| Version banner | `Dragonfly version 1793369 (...)`, which must equal the arm's version |
| Tick probe | `gametime` -> `Gametime is N` |
| In world | `Player spawned: <name>` |
| Positions | `positions` -> `Positions: N` and one `Position:` line per player; the reply is complete when N lines arrived |
| Stop | `stop` |
| Benign errors | none |

## Tick probe

`gametime` reads the overworld's current tick, a counter that only ever goes up and that Dragonfly advances once for
every tick it executes. It is not the world time (`World.Time()`), which stops when the daylight cycle is off.

Dragonfly skips the ticks of a world nobody is in. That saves work while the server is empty, but it also stops the tick
counter, so the probe could not tell a stalled server from an idle one. With `Bench.IdleTicking`, dfbench adds a loader
to the overworld that counts as a viewer: its chunk radius is 0, it is never asked to load a chunk and it sits far
away from every benchmark position, so the world ticks but has nothing more to tick. dfbench's tests check both
halves against the pinned Dragonfly: without the loader an empty world stays at tick 1, with it the counter advances.
During load and steady windows the bots keep every world they are in ticking anyway; the loader only matters while the
server is empty, or when every bot has left the overworld during chaos.

## Process and threads

`dfbench` is the measured process. A Go program schedules its goroutines on a pool of system threads, so there is no
tick thread to single out (`tick_thread_rule = "none"`): tick-thread metrics are not applicable, and process CPU is the
cost measure. The Go runtime sizes its pool (`GOMAXPROCS`) from the CPU set at startup; the harness starts the process
suspended and pins it before it runs, so the pool matches the server CPUs. `dfbench.json` records the value, and
`GOMAXPROCS`, `GOGC`, `GOMEMLIMIT` and `GODEBUG` from the user's environment never reach the server.

Things to know when reading the numbers:

- The world is flat (Dragonfly has no terrain generator). Chunk generation is nearly free compared with vanilla terrain,
  so runs of scenarios that exercise terrain are flagged `terrain_mismatch` and chunk work does not compare.
- Dragonfly accepts the positions clients send: there are no movement corrections, and bots teleported to y=300 keep
  hovering there (`airborne_frac_steady` near 1).
- There is no locator bar and there are no mobs.
- Worlds written by other servers do not load reliably (Nukkit's and PowerNukkitX's failed to decode or fell back to
  flat chunks); a template for `spread100-pregen` must come from Dragonfly itself.

## Chaos

dfbench renders `tp`, `tp_dimension`, `gamemode`, `effect_clear`, `kill`, `kick`, `list`, `weather`, `fill` and
`setblock`, so every action of `chaos-portable` runs. `summon`, `loot` and `cleanup` (no mobs or loot tables),
`randomtickspeed` (fixed when the world is created), `playerwaypoints` (no locator bar) and the ticking-area commands
are unsupported and counted as such.

## Crashes

dfbench writes no crash files. A Go panic prints its message and the goroutine stacks to the console, which the harness
keeps in `crash/server-log-tail.txt`.

## Updating Dragonfly

1. In `servers/dragonfly`, `go get github.com/df-mc/dragonfly@<commit or tag>` and `go mod tidy`. Fix dfbench if the
   API changed, then `go vet ./...` and `go test ./...` (with `GOWORK=off`).
2. Add the version to `artifacts/dragonfly.toml`: `module_version` and `go_sum` from `go.mod` and `go.sum`, and the
   sha256 and size of `https://proxy.golang.org/github.com/df-mc/dragonfly/@v/<module version>.zip`.
3. Add a range to `features/dragonfly.toml`, re-checking each class against the new source.
4. If `dfbench -defaults` changed, update `DEFAULTS` in `tests/unit/test_adapter_dragonfly.py`; with
   `BBENCH_DFBENCH=<path to dfbench>` that test compares them. If the rendered `config.toml` changed, update
   `servers/dragonfly/testdata/config.toml`.
5. `bbench fetch dragonfly@<version>` and `bbench smoke dragonfly@<version>`.
