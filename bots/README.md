# Load bot

The load bot joins simulated players to a Minecraft Bedrock server and reports what they receive. One process runs
every bot of a run. It writes events to stdout as JSON lines and takes commands on stdin. The harness
(`src/bedrock_bench/bots.py`) starts it, records the events in `bots.jsonl.gz` and stops it.

Every arm of a benchmark uses the same bot source and the same flags (rule R2 in `docs/METHODOLOGY.md`). The only
flag an adapter may add is `-fake-xuid`.

## Layout

| Module | Purpose |
|---|---|
| `core/` | Everything that does not depend on the protocol: flags, events, chunk accounting, the walk, identities, sub-chunk request ranges, the stdin protocol and the fleet that runs the bots. It does not import gophertunnel. |
| `loadbot/` | Protocol 2193 (Bedrock 1.26.5x), built on upstream [gophertunnel](https://github.com/Sandertv/gophertunnel) v1.62.0. Translates packets into `core.Session` calls. |
| `legacy1001/` | Protocol 1001, for PocketMine-MP only. Same core, older gophertunnel. |
| `patches/` | gophertunnel patches, when one is needed. See [PATCHES.md](PATCHES.md). |

Go cannot link two versions of one module into one binary, so each protocol is its own module and binary. A new
protocol frontend implements `core.Frontend`: one `RunSession` that dials, logs in, spawns, calls `Session.Spawned`
and then reports packets through the other `Session` methods until the session ends.

## Build

```sh
bbench bots build                          # every module, for this host
bbench bots build --target linux/amd64     # cross-compile
bbench bots fetch                          # release binary, checked against the release's SHA256SUMS
```

Binaries land in `bots/bin/<goos>-<goarch>/`, each with a `<name>.json` manifest that records its sha256 and its
`-version` output. The harness checks the hash before every run and records it in `run.json`.

The build needs Go 1.25 or newer; each `go.mod` pins `toolchain go1.26.3`, which Go downloads when the local
toolchain is older. Builds are reproducible: the same source and toolchain give the same bytes on any machine.
`bbench bots build` runs, inside the module directory:

```sh
CGO_ENABLED=0 GOFLAGS=-mod=readonly GOWORK=off \
  go build -trimpath -buildvcs=false -ldflags "-s -w -buildid= -X main.version=<harness version> -X main.commit=<commit>" -o <out> .
```

`main.commit` is the short commit of the checkout, with `-dirty` when `bots/` has uncommitted changes. Release
assets are named `<module>-<goos>-<goarch>[.exe]` and listed in `SHA256SUMS` on the release tagged `v<version>`.

`go.work` puts the modules in one workspace for development; the release build ignores it and uses each module's
own `go.mod` and `go.sum`.

## Running it by hand

```sh
loadbot -addr 127.0.0.1:19672 -n 4 -prefix a
```

Type `quit` (or press Ctrl+C) to stop; the bot then prints its summaries and exits. Ending stdin also stops it.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `-addr HOST:PORT` | required | Server address. There is no default port. |
| `-n` | 1 | Number of bots. |
| `-prefix` | `b` | Name prefix; bots are named prefix + index with at least two digits (`a00`, `a07`, `a123`). |
| `-stagger` | 300ms | Spacing between join starts. 100 bots take about 30 s to start. |
| `-csg` | false | Claim client-side chunk generation support and memory tier 4 in the login. |
| `-radius` | 16 | Chunk radius. The spawn sequence always requests 16; another value is requested right after spawn. Servers clamp it to their view distance. |
| `-loadscreen` | true | Send loading screen start and end after spawn and after every dimension change. Without the end message BDS never lets the player move (see the frozen-bot entry in `docs/PITFALLS.md`). |
| `-walk` | `circle` | `circle`: walk 0.2158 blocks per tick and turn 0.01 rad per tick (a circle about 43 blocks across every 628 ticks), at constant height. `none`: stand still. Input is sent at 20 Hz either way. |
| `-spawn-timeout` | 60s | Deadline for dial, login and spawn of one join attempt. Benchmark scenarios use 180s. |
| `-join-retries` | 0 | Extra attempts for a failed first join, 3 s apart. |
| `-fake-xuid` | false | Send XUID 2535400000000000 + index, for servers that derive the player UUID from the XUID. |
| `-identity-seed` | `bedrock-bench` | Name space of the bot UUIDs (below). |
| `-control` | false | Accept the JSON commands below on stdin. |
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

## Accounting

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

One JSON object per line on stdout, validated by `schemas/bot-event.schema.json`. Every event has `v` (1), `ev`,
`t` (UTC epoch milliseconds) and `mono_ms` (bot monotonic milliseconds since start); the harness adds `rx_ns` when
it stores the line. Human-readable diagnostics go to stderr.

| Event | Fields | When |
|---|---|---|
| `hello` | `build`, `flags`, `intervals_ms` | First line. `build` is the `-version` output; `flags` holds every flag's effective value. |
| `dial` | `bot`, `i` | A first-session join attempt starts. |
| `spawned` | `bot`, `i`, `session`, `join_ms`, `radius`, `dim` | A session reached the world. `join_ms` is measured on the bot clock from the attempt's start. |
| `join_failed` | `bot`, `i`, `stage` (`dial`, `login`, `spawn`), `err`, `session`, `attempt`, `final` | One failed attempt. `final` is true when the bot gives up; only final failures of the first session count as `failed`. |
| `tp` | `bot`, `pos`, `dim`, `n` | The server teleported the bot; `n` counts teleports in the session. |
| `first_chunk` | `bot`, `dt_ms` | See above. |
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

## stdin

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

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Stopped normally. |
| 1 | Bad flags. |
| 2 | Every first join failed. The bot stops by itself right after `alljoined`. |
| 3 | A decode error with `-decode-errors fail`. |

## Tests

```sh
cd bots/core && go test ./...
cd bots/loadbot && go test ./...
```

`core/testdata/golden.jsonl` pins the exact output of `-selftest-output` (regenerate with
`go test -run TestSelftestGolden -update`); `tests/unit/test_bots.py` validates it against the schema. The loadbot
tests include a loopback session against an in-process server built on gophertunnel's listener: login identity,
spawn, teleport, chunk and sub-chunk requests, a correction, latency probes, a decode error and a kick.
