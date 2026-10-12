# Load bots

The load bot joins simulated players to a Minecraft Bedrock server and reports what they receive. One process runs
every bot of a run. It writes events to stdout as JSON lines and takes commands on stdin. The harness
(`bedrock_bench/bots.py`) starts it, records the events in `bots.jsonl.gz` and stops it.

Every arm of a matrix uses the same bot source and the same flags (rule R2 in
[docs/methodology.md](../docs/methodology.md#rules)). The only flag an adapter may add is `-fake-xuid`.

| Module | Purpose |
|---|---|
| `core/` | Everything that does not depend on the protocol: flags, events, chunk accounting, the walk, identities, sub-chunk request ranges, the stdin protocol and the fleet that runs the bots. It does not import gophertunnel. |
| `loadbot/` | Protocol 2193 (Bedrock 1.26.5x), built on upstream [gophertunnel](https://github.com/Sandertv/gophertunnel) v1.62.0. Translates packets into `core.Session` calls. |
| `legacy1001/` | Protocol 1001, for PocketMine-MP only ([Protocol 1001](#protocol-1001)). Same core, gophertunnel v1.57.1 plus one [patch](#patches). |

Go cannot link two versions of one module into one binary, so each protocol is its own module and binary. A new
protocol frontend implements `core.Frontend`: one `RunSession` that dials, logs in, spawns, calls `Session.Spawned`
and then reports packets through the other `Session` methods until the session ends.

## Building

```sh
bbench bots build                          # every module, for this host
bbench bots build --module loadbot         # one module
bbench bots build --target linux/amd64     # cross-compile
bbench bots fetch                          # loadbot from the release, checked against its SHA256SUMS
bbench bots fetch --module legacy1001      # the PocketMine-MP bot
```

Binaries land in `bots/bin/<goos>-<goarch>/`, each with a `<name>.json` manifest that records its sha256 and its
`-version` output. The harness hashes the binary before every run and records it in `run.json`.

The build needs Go 1.25 or newer; each `go.mod` pins `toolchain go1.26.3`, which Go downloads when the local
toolchain is older. `bbench bots build` runs, inside the module directory:

```sh
CGO_ENABLED=0 GOFLAGS=-mod=readonly GOWORK=off \
  go build -trimpath -buildvcs=false -ldflags "-s -w -buildid= -X main.version=<harness version> -X main.commit=<commit>" -o <out> .
```

`main.commit` is the short commit of the checkout, with `-dirty` when `bots/` has uncommitted changes.

Release binaries come from `.github/scripts/build-go.sh`. It uses the same flags plus `GOAMD64=v1`, stamps the
`git describe` version and the full commit, and builds `linux/amd64` and `windows/amd64`. The `bots.yml` workflow runs
it on Linux and on Windows and fails when the two `SHA256SUMS` differ. The release tagged `v<version>` carries
`<module>-<goos>-<goarch>[.exe]` and `SHA256SUMS`. A local `bbench bots build` stamps different values, so its bytes
differ from the release; `bbench bots fetch` gets the release bytes.

`go.work` puts `core` and `loadbot` in one workspace for development. Builds ignore it and use each module's own
`go.mod` and `go.sum`.

Tests, per module:

```sh
cd bots/core && GOWORK=off go vet ./... && GOWORK=off go test ./...
cd bots/loadbot && GOWORK=off go vet ./... && GOWORK=off go test ./...
```

`core/testdata/golden.jsonl` pins the exact output of `-selftest-output` (regenerate with
`go test -run TestSelftestGolden -update`); `tests/unit/test_bots.py` validates it against the schema. The loadbot
tests include a loopback session against an in-process server built on gophertunnel's listener: login identity,
spawn, teleport, chunk and sub-chunk requests, a correction, latency probes, a decode error and a kick.

## Flags

```sh
loadbot -addr 127.0.0.1:19672 -n 4 -prefix a
```

Type `quit` (or press Ctrl+C) to stop; the bot then prints its summaries and exits. Ending stdin also stops it. In a
run, the harness builds the flags from the scenario (`configs/scenarios/`) and records the full argv in `run.json`.

| Flag | Default | Meaning |
|---|---|---|
| `-addr HOST:PORT` | required | Server address. There is no default port. |
| `-n` | 1 | Number of bots. |
| `-prefix` | `b` | Name prefix; bots are named prefix + index with at least two digits (`a00`, `a07`, `a123`). |
| `-stagger` | 300ms | Spacing between join starts. 100 bots take about 30 s to start. |
| `-csg` | false | Claim client-side chunk generation support and memory tier 4 in the login. |
| `-radius` | 16 | Chunk radius. The spawn sequence always requests 16; another value is requested right after spawn. Servers clamp it to their view distance. |
| `-loadscreen` | true | Send loading screen start and end after spawn and after every dimension change. Without the end message BDS keeps the player on its loading screen and ignores its movement: the bot never falls, gets no corrections and receives no new chunks. |
| `-walk` | `circle` | `circle`: walk 0.2158 blocks per tick and turn 0.01 rad per tick (a circle about 43 blocks across every 628 ticks), at constant height. `none`: stand still. Input is sent at 20 Hz either way. |
| `-spawn-timeout` | 60s | Deadline for dial, login and spawn of one join attempt. |
| `-join-retries` | 0 | Extra attempts for a failed first join, 3 s apart. |
| `-fake-xuid` | false | Send XUID 2535400000000000 + index, for servers that derive the player UUID from the XUID. |
| `-identity-seed` | `bedrock-bench` | Name space of the bot UUIDs ([What a bot does](#what-a-bot-does)). |
| `-control` | false | Accept the JSON commands on stdin ([Commands on stdin](#commands-on-stdin)). |
| `-rejoin-on-drop` | false | Rejoin 3 s after a kick or a lost connection. Chaos scenarios only. |
| `-radius-jitter` | 0 | Re-request a random radius in 4..16 at this mean interval (uniform in 0.5x..1.5x). Chaos scenarios only. |
| `-jitter-paused` | false | Keep radius jitter off until `{"op":"jitter","on":true}`. |
| `-progress-interval` | 5s | Interval of `progress` events. |
| `-stat-interval` | 10s | Interval of `stat` events (and `rtt` events when probing). |
| `-pkt-interval` | 60s | Interval of `pkt` events. |
| `-probe-bots` | 0 | The bots with the lowest indices send a latency probe every second. |
| `-decode-errors` | `count` | `count`: report and continue. `fail`: stop and exit 3 at the first. |
| `-selftest-output` | | Print one event of every kind with made-up values and exit. Used to check the schema. |
| `-version` | | Print build information as JSON and exit. |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Stopped normally. |
| 1 | Bad flags. |
| 2 | Every first join failed. The bot stops by itself right after `alljoined`. |
| 3 | A decode error with `-decode-errors fail`. |

## What a bot does

- **Login.** Offline mode, self-signed, over RakNet. The UUID is UUIDv5(name space, bot name), where the name space
  is `-identity-seed` itself when it is a UUID and UUIDv5(the RFC 9562 URL name space, seed) otherwise. In Python:
  `uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_URL, "bedrock-bench"), "a07")`. Identities are the same in every run, so
  servers that keep player data see the same players. Resource packs are never downloaded.
- **Chunks.** The client blob cache is on and every blob is reported missing, so the server always sends full chunk
  data: the most work a server can be asked for. When a chunk asks for sub-chunk requests, the bot requests exactly
  the announced number of sub-chunks from the bottom of the dimension (overworld from -4, nether and end from 0), or
  the whole column when no limit is given.
- **Movement.** The bot starts with yaw = index x 137.508 degrees, so neighbouring bots walk in different
  directions. It sends one `PlayerAuthInput` per tick with its position; it never changes its own height. A server
  teleport is acknowledged in the next input; a correction replaces the bot's position.
- **Upkeep.** The bot answers latency requests from the server, asks to respawn after a death (up to four times,
  10 s apart), and acknowledges dimension changes one second after they arrive.

### Accounting

- A **chunk** is a column keyed by (dimension, x, z). Chunks received before the bot's first teleport belong to the
  spawn area and are ignored.
- **uniq** is the number of distinct chunks a session received after its first teleport.
- **in_view** is the number of those chunks within radius + 1 chunks (Euclidean) of the chunk the bot stands in,
  in its current dimension, using the radius the server granted. Chunks of an old view that a server keeps
  streaming after a teleport do not count. A disc of radius 10 holds 317 columns.
- **first_chunk** is the time from a session's first teleport to the first chunk that is in view.
- **corrections** counts movement corrections from the server.
- **rx_bytes** and **tx_bytes** count UDP payload bytes, RakNet framing and resends included.
- A **decode error** is a packet or batch the bot could not decode. Socket errors when a server goes away are not
  decode errors; they end the session as `dropped`. Decode errors make a run invalid.

Per-bot numbers (`progress`, `bot_summary`, `first_chunk`) describe one session and start from zero after a
rejoin. Fleet counters in `stat` add up over all sessions.

## Events

One JSON object per line on stdout, validated by `bedrock_bench/schemas/bot-event.schema.json`. Every event has `v`
(1), `ev`, `t` (UTC epoch milliseconds) and `mono_ms` (bot monotonic milliseconds since start); the harness adds
`rx_ns` when it stores the line. Human-readable diagnostics go to stderr.

| Event | Fields | When |
|---|---|---|
| `hello` | `build`, `flags`, `intervals_ms` | First line. `build` is the `-version` output; `flags` holds every flag's effective value. |
| `dial` | `bot`, `i` | A first-session join attempt starts. |
| `spawned` | `bot`, `i`, `session`, `join_ms`, `radius`, `dim` | A session reached the world. `join_ms` is measured on the bot clock from the attempt's start. |
| `join_failed` | `bot`, `i`, `stage` (`dial`, `login`, `spawn`), `err`, `session`, `attempt`, `final` | One failed attempt. `final` is true when the bot gives up; only final failures of the first session count as `failed`. |
| `tp` | `bot`, `pos`, `dim`, `n` | The server teleported the bot; `n` counts teleports in the session. |
| `first_chunk` | `bot`, `dt_ms` | See [Accounting](#accounting). |
| `progress` | `rows` | Every progress interval: `[bot, uniq, in_view, x, y, z, dim, corrections, teleports]` per bot that has started. Position and dimension are null until the bot's first teleport. |
| `stat` | see below | Every stat interval. |
| `pkt` | `rx`, `tx` | Packets received and sent since the previous `pkt`, by gophertunnel packet name (`LevelChunk`, `MoveActorDelta`, ...). |
| `rtt` | `n`, `p50_ms`, `p99_ms`, `max_ms`, `lost` | Latency probes since the previous `rtt`, nearest-rank percentiles. Only with `-probe-bots`. |
| `died`, `respawned` | `bot`, `session` | Death and respawn. |
| `dim` | `bot`, `dim`, `session` | Dimension change. |
| `left` | `bot`, `session` | A `reconnect` or `disconnect` command closed the session. |
| `rejoin` | `bot`, `session` | A rejoin attempt starts (sessions after the first). |
| `dropped` | `bot`, `session`, `err` | The connection failed. |
| `kicked` | `bot`, `session`, `reason` | The server disconnected the bot; `reason` is the server's message. |
| `decode_error` | `bot`, `pkt_id`, `err`, `total` | `pkt_id` is null when the packet is unknown. The first 10 are reported, then at most one per second; `total` and `stat.decode_errors` count all of them. |
| `alljoined` | `joined`, `failed` | Once, when every bot has spawned or finally failed its first join. |
| `bot_summary` | `bot`, `uniq`, `in_view`, `chunks`, `corrections`, `first_seen_ms` | At shutdown, for each bot's last session that reached the world. `chunks` counts chunk packets after the first teleport, resends included. `first_seen_ms` holds one entry per chunk in view, in arrival order: milliseconds since the first teleport, delta-encoded. |
| `done` | | Last line. |

`stat` fields: `online` (sessions in the world now), `joined`, `failed`, `dropped` (sessions the bot did not end,
kicks included), `kicked`, `left`, `rejoins`, `rejoin_failed`, `deaths`, `rx_pkts`, `tx_pkts`, `rx_bytes`,
`tx_bytes`, `corrections`, `level_chunks` (every chunk packet, spawn area included), `subchunks` (sub-chunk entries),
`uniq` (summed over sessions), `in_view` (now, summed over online bots) and `decode_errors`. All but `online` and
`in_view` are cumulative.

### Commands on stdin

`quit` is always accepted. With `-control`, each line may also be one JSON command. A bot is addressed by name
(`"bot"`) or index (`"i"`).

| Command | Effect |
|---|---|
| `{"op":"reconnect","bot":"a07"}` | Close the session (`left`) and rejoin after `delay_ms` (default 3000). Also brings back a bot that was disconnected. |
| `{"op":"disconnect","i":7}` | Close the session (`left`) and stay offline until a `reconnect`. |
| `{"op":"jitter","on":true}` | Turn radius jitter on or off. |
| `{"op":"radius","radius":8}` | Request a chunk radius, for one bot when addressed, otherwise for every online bot. |
| `{"op":"stop"}` | Same as `quit`. |

Malformed or unknown lines are ignored with a note on stderr.

Shutdown (`quit`, `stop`, end of stdin, SIGINT, SIGTERM, Ctrl+Break, or a failed write to stdout) writes a last
`progress`, `stat`, `pkt` (and `rtt`), closes every session within 10 s, then writes `bot_summary` for each bot and
`done`.

## Protocol 1001

`legacy1001` is the bot for servers that speak protocol 1001. In the matrices that is `pocketmine@5.44.3`; every
other server gets `loadbot`. Runs with it are flagged `bot_protocol_differs` (rule R2).

It is `loadbot` built against gophertunnel v1.57.1, the last release for protocol 1001. PocketMine-MP 5.44.3 rejects
that release's offline login, so the module carries the login back-port listed under [Patches](#patches). Upstream
has no release that speaks protocol 1001 with the newer login, so there is nothing to wait for upstream. Flags,
events, the stdin protocol and all bot behaviour come from `core`. The non-test files mirror `loadbot`'s one for one;
the package comment in `legacy1001/main.go` lists where the older gophertunnel API makes them differ. A behaviour
change to `loadbot` must be made here too.

```sh
cd bots/legacy1001
GOWORK=off go vet ./...
GOWORK=off go test ./...
```

`GOWORK=off` is required. `go.work` covers `core` and `loadbot` only, because a workspace resolves one gophertunnel
version for all its modules. The module downloads gophertunnel from the fork tag described under
[Patches](#patches), so that tag must be published before it builds.

`TestLoginRequestFormat` checks the login a server receives against the format PocketMine-MP expects, and fails
against unpatched v1.57.1. `TestLoopbackSession` runs a whole session against an in-process protocol 1001 server.
Neither needs PocketMine-MP; the real-server check is `bbench smoke pocketmine@5.44.3` (4 bots) with no decode
errors.

## Patches

`loadbot` builds against upstream gophertunnel v1.62.0 with no `replace` directive and carries no patches.
`legacy1001` carries one:

| Patch | Change | Upstream | Failing case | Drop when |
|---|---|---|---|---|
| `legacy1001/0001-login-backport.patch` | The offline login in the format gophertunnel adopted in v1.58.0: the self-signed identity token gets the multiplayer audience and carries the player UUID as `leguuid`, and the client data sends `FilterProfanity`, `ClientEditorConnectionIntent`, `ClientIsEditorCapable` and `ProfileHash` instead of `IsEditorMode`. | Back-port of [#468](https://github.com/Sandertv/gophertunnel/pull/468) (654cd5f) and the `ProfileHash` field of [#481](https://github.com/Sandertv/gophertunnel/pull/481) (b36ddad), both released in v1.58.0 | `TestLoginRequestFormat` | The pinned PocketMine-MP accepts the protocol `loadbot` speaks. `legacy1001` is removed with it. |

Upstream changes to the same files that the back-port leaves out:

- `ClientData.Nonce` ([#501](https://github.com/Sandertv/gophertunnel/pull/501)). It is `omitempty` and the bot
  never sets it, so it never reaches the wire.
- The login parsing changes made before v1.58.0 (#474 and the `Validate` part of #481). The bot never parses a login.

Neither bot carries the changes of the modified gophertunnel snapshot the legacy harness used. The radius is
requested with a normal `RequestChunkRadius` packet after spawn (`-radius`), and `TestSubChunkHeightMapWireLayout` in
`loadbot` shows that upstream decodes the sub-chunk height maps the snapshot patched. If a pinned server sends
something upstream cannot decode, the bot reports a `decode_error` naming the packet, the run is invalid, and the
fix goes through the policy below.

### Policy

A patch is carried only when all of these exist:

1. **A failing case.** A test in the bot module that fails against upstream, usually a decode test built from the
   bytes a pinned server sends, and a reproduction: the bot with `-decode-errors fail` exits 3 against that server.
2. **An upstream report.** An issue or pull request on gophertunnel, linked in the table above.
3. **A drop condition.** The upstream release, or server change, that makes the patch unnecessary.

A carried patch lives in three places that must agree:

- `bots/<module>/NNNN-<topic>.patch`, applied in order to the upstream tag;
- a public fork tag `v<upstream version>-bench.<N>` of `github.com/EndstoneMC/gophertunnel` holding upstream plus
  exactly those patches, referenced by a `replace` in the module's `go.mod`;
- `carriedPatches` in the module's `main.go`, so `-version`, the `hello` event and `run.json` list it.

The module's `go.sum` pins the hash of the tag's tree, so Go refuses a tag with any other content: the download
fails with a checksum mismatch. CI does not rebuild the tag from upstream and the patch files; whoever publishes a
tag applies the patches to a clean upstream checkout, as below. Patches keep gophertunnel's MIT license and
copyright.

Publishing the `legacy1001` tag:

```sh
git -c core.autocrlf=false clone https://github.com/EndstoneMC/gophertunnel
cd gophertunnel
git switch --detach v1.57.1
git apply ../bedrock-bench/bots/legacy1001/0001-login-backport.patch
git commit -am "Apply bedrock-bench patches for protocol 1001"
git tag v1.57.1-bench.1
git push origin v1.57.1-bench.1

cd ../bedrock-bench/bots/legacy1001
GOWORK=off go mod download   # a go.sum mismatch means the tag is not upstream + patches
GOWORK=off go test ./...
```

A pushed tag is permanent: the Go module proxy and checksum database keep the first content they see. To change the
patches, edit the files in `bots/legacy1001/`, publish the next tag (`v1.57.1-bench.2`), point the `replace` in its
`go.mod` at it, run `GOWORK=off go mod tidy` there, and update `carriedPatches` in its `main.go`, all in one commit.

Before each release the maintainer runs a decode smoke against the real servers, because CI never downloads BDS; see
[.github/CONTRIBUTING.md](../.github/CONTRIBUTING.md#releasing).
