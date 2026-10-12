# 0001. One bot source for every server

- Status: accepted
- Date: 2026-10-12

## Context

The load generator decides what work a server is asked to do: how often clients send input, which chunks they request,
whether they acknowledge cached chunks, how they move. The legacy benchmark used several bot binaries: a steady-state
build whose source was not kept, a chaos build, a Linux chaos build that jittered its view radius from the moment it
joined, a build with fake XUIDs for Nukkit and PowerNukkitX, and a protocol 1001 build for PocketMine-MP. Their flags
also differed per server. A difference between two servers' results could therefore come from the bots as much as from
the servers.

Bedrock servers do not all speak the same protocol version. PocketMine-MP 5.44 supports only protocol 1001, while BDS
1.26.52 uses 2193. Go cannot link two versions of the protocol library (gophertunnel) into one module.

## Decision

- All bot behaviour lives in one Go module, `bots/core`, which has no protocol dependency: identity, walking, chunk
  accounting, sub-chunk ranges, the stdin control protocol, counters and the JSON event writer.
- Each protocol gets a thin frontend that translates packets to core events: `bots/loadbot` (protocol 2193, upstream
  gophertunnel) and `bots/legacy1001` (protocol 1001, PocketMine-MP only).
- Every arm of a matrix runs the bot with identical flags (rule R2). Adapters may add only allowlisted identity flags
  (currently `-fake-xuid`). The contract test enforces the allowlist.
- Patches to gophertunnel are kept only when a failing test proves them necessary, each documented in
  `bots/PATCHES.md` with an upstream link and a condition for dropping it.
- Runs with the protocol 1001 frontend are flagged `bot_protocol_differs`.
- The bot build (module, version, commit, Go and gophertunnel versions, patches, binary sha256) is recorded in every
  run. Release binaries are built reproducibly and checked by a double build.

## Consequences

- A behaviour change reaches every server at once, and a result can always be traced to one bot source and build.
- PocketMine-MP results still differ in one respect, the protocol version, and the report says so.
- Supporting another protocol means writing a frontend, not a new bot.
- Compatibility needs of individual servers (UUIDs derived from XUIDs, for example) must be met with allowlisted flags
  or adapter-side config, never by changing the bot's behaviour for one server.
