# legacy1001

The load bot for servers that speak protocol 1001. In the benchmark that is `pocketmine@5.44.3`; every other server
gets `loadbot`. Runs with this bot are flagged `bot_protocol_differs` (rule R2 in
[`docs/METHODOLOGY.md`](../../docs/METHODOLOGY.md)).

It is `loadbot` built against gophertunnel v1.57.1, the last release for protocol 1001, with the patch in
[`bots/patches/gophertunnel-v1.57.1`](../patches/gophertunnel-v1.57.1/README.md). Flags, events, the stdin protocol
and all bot behaviour come from `bots/core` and are documented in [`bots/README.md`](../README.md).

The source files mirror `bots/loadbot` one for one, so `diff -r bots/loadbot bots/legacy1001` shows only what the
older gophertunnel API needs:

- A chunk in sub-chunk request mode is marked by `LevelChunk.SubChunkCount` (with `HighestSubChunk` in limited mode)
  instead of `SubChunkLimit`.
- Sub-chunk blob hashes are plain values when `SubChunk.CacheEnabled` is set, not optional ones.
- `PlayerAuthInput` flags are a `protocol.NewBitset(packet.PlayerAuthInputBitsetSize)`.
- A kick ends the session with a closed-connection error that carries the server's message, not a `DisconnectError`.

A change to `loadbot`'s behaviour belongs here too.

## Build and test

```sh
cd bots/legacy1001
GOWORK=off go vet ./...
GOWORK=off go test ./...
```

`GOWORK=off` is required. `bots/go.work` covers `core` and `loadbot` only, because a workspace resolves one
gophertunnel version for all its modules. Release binaries come from `bbench bots build --module legacy1001`, with the
flags in [`bots/README.md`](../README.md#build).

`TestLoginRequestFormat` checks the login a server receives against the format PocketMine-MP expects, and fails
against unpatched gophertunnel v1.57.1. `TestLoopbackSession` runs a whole session against an in-process protocol 1001
server. Neither needs PocketMine-MP; the real-server check is a 4-bot `bbench smoke pocketmine@5.44.3` with no decode
errors.
