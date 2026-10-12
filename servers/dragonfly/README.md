# Dragonfly (`dragonfly`)

[Dragonfly](https://github.com/df-mc/dragonfly) is a Bedrock server written in Go, licensed under MIT. It shares no code
with Mojang's server: terrain, movement, entities and block behaviour are its own, and much of vanilla is absent. Its
`[features]` rows in [server.toml](server.toml) are fidelity V1, so reports list it under "Reduced fidelity". It needs no
EULA, so it is the one real server CI runs: a 4-bot smoke in
[e2e-dragonfly.yml](../../.github/workflows/e2e-dragonfly.yml).

Dragonfly is a library with an example server and no binary releases. This folder is the Go module of dfbench:
Dragonfly's example server plus a console on standard input. Adapter:
[dragonfly.py](../../bedrock_bench/adapters/dragonfly.py).

| File | |
|---|---|
| `main.go` | flags, server start, version banner, idle ticking |
| `config.go` | `config.toml` loading and the `[Bench]` table |
| `console.go` | console commands |
| `server.toml` | the pinned Dragonfly module, declared parity, and what the harness writes into the run's `config.toml` |
| `testdata/config.toml` | the `config.toml` the harness writes for `spread100`; Go and Python tests both check it |

```shell
GOWORK=off go vet ./... && GOWORK=off go test ./...
go run . -version     # build information as JSON
go run . -defaults    # the default configuration as TOML
```

## Setup

```shell
bbench fetch dragonfly@1793369
```

Fetch needs the Go toolchain on PATH (`go.mod` names `toolchain go1.26.3`, which Go downloads if the installed one is
older) and access to the Go module proxy. No licence flag is needed. It:

1. checks that `go.mod` requires the module version pinned in [server.toml](server.toml), that `go.sum` records the
   pinned hash, and that `go.mod` has no `replace` directive, because a benchmark builds published Dragonfly code only;
2. downloads the module with `go mod download` and checks its `go.sum` hash and the sha256 and size of the zip against
   the pin;
3. builds dfbench the way the bots are built (`CGO_ENABLED=0`, `GOWORK=off`, `-mod=readonly`, `GOAMD64=v1`,
   `-trimpath -buildvcs=false -ldflags "-s -w -buildid= ..."`), so the same source and Go version give the same binary;
4. runs `dfbench -version`, which must report the pinned Dragonfly version and the bots' protocol, and
   `dfbench -defaults`, which records the build's default configuration next to the binary.

The install is cached read-only and keyed by the Dragonfly version, the target, the hash of `go.mod`, `go.sum` and the
non-test Go files, the Go version and the harness version, so editing dfbench or upgrading Go rebuilds it. Each run
gets a fresh copy with no world folder.

To measure a dfbench built elsewhere, for example against a Dragonfly fork, pass it as the arm's artifact:
`bbench smoke configs/smoke.toml dragonfly@devel --artifact path/to/dfbench`. A dfbench whose Dragonfly comes
from a `replace` directive or a Go workspace reports Dragonfly version `devel`, so its banner matches only an arm
tagged `devel`, and the run is marked `unpublished_artifact`.

### Adding a release

A build from a commit is tagged with the commit's first seven characters, a release with its version
(`dragonfly@0.11.5`).

1. In this folder, `go get github.com/df-mc/dragonfly@<commit or tag>` and `go mod tidy`. Fix dfbench if the API
   changed, then run `go vet` and `go test` as above.
2. Add the version to `[artifacts]` in [server.toml](server.toml): `module_version` and `go_sum` from `go.mod` and
   `go.sum`, and the sha256 and size of `https://proxy.golang.org/github.com/df-mc/dragonfly/@v/<module version>.zip`.
3. Add a range to `[features]`, re-checking each class against the new source.
4. If `dfbench -defaults` changed, update `DEFAULTS` in `tests/unit/test_adapter_dragonfly.py`; with
   `BBENCH_DFBENCH=<path to dfbench>` that test compares them. If the rendered `config.toml` changed, update
   `testdata/config.toml`.
5. `bbench fetch dragonfly@<version>` and `bbench smoke configs/smoke.toml dragonfly@<version>`.

## How a run starts

```
dfbench -config config.toml
```

- It runs in the run's server directory. `GOMAXPROCS`, `GOGC`, `GOMEMLIMIT` and `GODEBUG` are removed from the
  environment.
- dfbench is the measured process. Go schedules goroutines on a pool of system threads, so there is no tick thread to
  single out (tick thread rule `none`): tick-thread metrics are not applicable, and process CPU is the cost measure. The
  Go runtime sizes its pool (`GOMAXPROCS`) from the CPU set at start; the harness pins the process before it runs, so
  the pool matches the server CPUs.
- Tick probe: `gametime` prints `Gametime is N`, the overworld's current tick, which Dragonfly advances once for every
  tick it executes. It is not the world time, which stops when the daylight cycle is off.
- Positions: `positions` prints `Positions: N`, then one `Position: <dimension id> <x> <y> <z> <name>` line per player.
- The ready line is dfbench's `Server started.`, printed after the listener is up; Dragonfly's own
  `Dragonfly server started.` comes earlier.
- The banner `Dragonfly version <version> (module ..., Minecraft ..., protocol ..., go..., GOMAXPROCS n)` must equal
  the arm's version.
- dfbench prints `Player spawned: <name>` once a player is in the world. Dragonfly prints nothing when a player joins,
  and a player that has connected is not yet in the world, so earlier teleports and effects would go nowhere.
- At start, dfbench writes `dfbench.json` into the server directory: build information, `GOMAXPROCS`, the CPU count Go
  sees, and the world settings in effect, with difficulty and game mode read back from the world. The adapter records
  it in run.json (`server.facts`).

### Console commands

Commands that touch the world run as tasks on the world or player that owns the data, so a slow tick delays the reply,
never the console. Replies are logged at info level; a command that fails says why at error level, which
`bbench smoke` treats as a failed command. A target is a player name, in any case, or `@a`; names with spaces go in
double quotes. End of input stops the server, so a harness that goes away takes the server with it.

| Command | Reply |
|---|---|
| `gametime` | `Gametime is N` |
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

### The `[Bench]` table

Settings Dragonfly only exposes through its Go API. With no `[Bench]` table, dfbench behaves like the example server,
including writing Dragonfly's default `config.toml` when the file is missing.

| Key | Default (example server) | Meaning |
|---|---|---|
| `LogLevel` | `debug` | lowest level logged: `debug`, `info`, `warn`, `error` |
| `IdleTicking` | `false` | keep the overworld ticking while nobody is in it (see Caveats) |
| `SaveIntervalSeconds` | `600` | autosave interval; 0 keeps Dragonfly's default, negative turns autosave off |
| `TickRange` | from `level.dat` (6 in a new world) | chunks around each player whose blocks and entities tick |
| `Difficulty` | from `level.dat` (`normal` in a new world) | `peaceful`, `easy`, `normal`, `hard` |
| `GameMode` | from `level.dat` (`creative` in a new world) | game mode of players joining for the first time |

## Configuration changes

The values and reasons are in the `[config]` table of [server.toml](server.toml), and every change is recorded in
run.json with its category ([methodology](../../docs/methodology.md#fairness-and-feature-parity)). Only those keys
are written into the run's `config.toml`; every other key keeps the build's default as printed by `dfbench -defaults`.

- harness: listen on `127.0.0.1` at the machine profile's port, so the bots connect over loopback and the newly built
  binary never triggers a firewall prompt; the server name; offline login; log level `info`, because at `debug`
  Dragonfly logs every packet it does not handle; `Bench.IdleTicking` on (see Caveats).
- parity: view distance (`Players.MaximumChunkRadius`, 32 by default), simulation distance (`Bench.TickRange`),
  difficulty, max players, world folder, and the autosave interval in seconds. `Bench.GameMode` is the scenario's game
  mode, because a new Dragonfly world puts players in creative, where they take no damage and may fly.
- `World.SaveData` and `Players.SaveData` are written as `true` and read back. Dragonfly saves world and player data by
  default in this build, so neither is an override.

Left at their defaults: the RakNet transport, join and quit messages (printed to every player and to the console), and
Dragonfly's random tick speed of 3, which is fixed when the world is created. Nothing is `rescue` or `nonvanilla`.

## Caveats

- The world is flat: Dragonfly has no other generator, so there is no seed to align. Generating a chunk is nearly free
  compared with vanilla terrain, so runs of scenarios that exercise terrain are flagged `terrain_mismatch` and chunk
  work does not compare.
- Dragonfly accepts the positions clients send: it sends no movement corrections, and bots teleported to the spread
  height keep hovering there. There is no locator bar and there are no mobs. `corrections_per_bot_s`,
  `locator_pkts_per_bot_s` and `airborne_frac_steady` check this in every run.
- Dragonfly skips the ticks of a world nobody is in, which also stops its tick counter, so the probe could not tell a
  stalled server from an idle one, and the `probe_monotonic` gate would fail during idle. With `Bench.IdleTicking`,
  dfbench adds a loader to the overworld that counts as a viewer: its chunk radius is 0, it is never asked to load a
  chunk and it sits far from every benchmark position, so the world ticks but has nothing more to tick. dfbench's
  `TestIdleTicking` checks both halves against the pinned Dragonfly. Once bots are in a world they keep it ticking
  anyway; the loader matters while the server is empty.

## Crash files

dfbench writes no crash files. A Go panic prints its message and the goroutine stacks to the console. When the server
dies, exits nonzero or has to be killed at stop, the harness keeps the last log lines in `crash/server-log-tail.txt`.

dfbench is Apache-2.0, like the rest of the repository, and links Dragonfly (MIT); see [NOTICE](../../NOTICE).
