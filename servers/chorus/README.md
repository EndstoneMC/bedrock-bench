# Chorus (`chorus`)

[Chorus](https://github.com/bedrock-crustaceans/chorus) is a Bedrock server written in Rust on
[bedrock-rs](https://github.com/bedrock-crustaceans/bedrock-rs) and Bevy ECS, licensed under Apache-2.0. It shares no
code with Mojang's server: terrain, chunks, movement and commands are its own, and most vanilla gameplay is absent. Chorus
has no releases or tags, so a version is a commit, built from source.

Chorus is a library and a server binary. This folder is the Cargo package of chorusbench: the Chorus server plus the
console commands the harness needs. Adapter: [chorus.py](../../bedrock_bench/adapters/chorus.py).

| File | |
|---|---|
| `main.rs` | flags, version banner, console commands, `chorusbench.json` |
| `build.rs` | embeds the Chorus commit from `Cargo.lock` and the compiler version |
| `Cargo.toml` | Chorus at the pinned commit |
| `Cargo.lock` | every git dependency by commit and every crate by checksum |
| `server.toml` | the pinned commit, toolchain and lock hash, and the `bbench run` default |
| `chorus.toml` | template of the run's `chorus.toml` |

```shell
cargo +1.95.0 build --release --locked
target/release/chorusbench -version     # build information as JSON
target/release/chorusbench -defaults    # the default chorus.toml
```

## Setup

```shell
bbench fetch chorus@e712f78
```

Fetch needs Rust 1.95.0 (with rustup, `rustup toolchain install 1.95.0`; fetch selects it through
`RUSTUP_TOOLCHAIN`), a C compiler for the bundled zstd (the MSVC build tools on Windows, which the Rust toolchain needs
anyway, or `cc` on Linux), and access to GitHub and crates.io. No licence flag is needed. It:

1. checks that `Cargo.toml` names the commit pinned in [server.toml](server.toml), that it patches no dependency, that
   `Cargo.lock` has the pinned sha256 and locks Chorus at that commit, and that `rustc` is the pinned version;
2. builds chorusbench with `cargo build --release --locked` into a scratch target directory, with `RUSTFLAGS`, Cargo
   profile overrides and target settings removed from the environment, so the committed manifest decides the build;
3. runs `chorusbench -version`, which must report the pinned commit, Minecraft version and the bots' protocol, and
   `chorusbench -defaults`, which records the build's default `chorus.toml` next to the binary.

A fresh build takes a few minutes. On Windows the cache path must stay short: Cargo's build paths under it have to fit
in 260 characters, and with a long `BBENCH_CACHE` the build fails with `LNK1104` on a build script. The default cache
location is fine.

The install is cached read-only and keyed by the version, the hash of `Cargo.toml`, `Cargo.lock` and the Rust files,
the `rustc` version and the harness version. Each run gets a fresh copy with no world folder. To measure a chorusbench
built elsewhere, pass it as the arm's artifact with `--artifact`; its `-version` must still name the arm's commit, and
the run is marked `unpublished_artifact`.

### Adding a commit

1. In `Cargo.toml`, set `rev` to the new commit, then `cargo +<rust> update` and review the `Cargo.lock` diff. Fix
   `main.rs` if the Chorus API changed.
2. Add the version to `[artifacts]` in [server.toml](server.toml): the commit, protocol, Minecraft version, the Rust
   version that builds it, the sha256 of `Cargo.lock` with LF line endings, and the sha256 and size of the commit's
   source archive.
3. If `chorusbench -defaults` changed, update `DEFAULTS` in `tests/unit/test_adapter_chorus.py`; with
   `BBENCH_CHORUSBENCH=<path to chorusbench>` that test compares them.
4. `bbench fetch chorus@<version>`, then `loadbot -decode-errors fail` and
   `bbench smoke configs/smoke.toml chorus@<version>`.

## How a run starts

```
chorusbench
```

- It runs in the run's server directory and reads `chorus.toml` there. Without that file it would write the defaults
  and listen on 19132.
- chorusbench is the measured process. Chorus runs its tick schedule on the main thread, the first thread of the
  process (tick thread rule `first_created`). Chunk generation, serialisation and storage run on Bevy's compute,
  async compute and IO task pools, listed as thread groups.
- Tick probe: `gametime` prints `Gametime is N`, the number of ticks Chorus has run. It advances once per executed tick.
- Positions: `positions` prints `positions: N`, then one `position: <name> <x> <y> <z> <dimension>` line per player,
  at foot height.
- The ready line is `Started on <ip>:<port>.`. The banner `chorusbench <commit> (chorus <version>, Minecraft ...,
  protocol ..., rustc ...)` must name the arm's commit; Chorus's own `Chorus v0.0.1 1.26.50 (2193)` follows it.
- A player is in the world at `<name> joined the game`, which Chorus logs when the client reports it has spawned.
- At start, chorusbench writes `chorusbench.json` into the server directory: build information, the thread count in
  effect and how the task pools split it, and the chunk saving, autosave, transport and compression settings in
  effect. The adapter records it in run.json (`server.facts`).

### Console commands

Log lines are `HH:MM:SS LEVEL text`, with span names such as `network` or `registry` left in the text. Replies to
commands have no prefix, and Chorus prints a failing command's message the same way. chorusbench's own commands log
their failures at error level, which `bbench smoke` treats as a failed command. A player is named in any case; names
with spaces go in double quotes. Standard input is read without a terminal; when it ends, the server keeps running.

| Command | Reply |
|---|---|
| `gametime` | `Gametime is N` (chorusbench) |
| `positions` | `positions: N`, then `position: <name> <x> <y> <z> <dimension>` per player (chorusbench) |
| `tpp <player> <x> <y> <z>` | `Teleported <name> to <x>, <y>, <z>` (chorusbench) |
| `kick <player> [reason]` | `Kicked <name>: <reason>` (chorusbench) |
| `list` | `There are N/M players online:`, then the names |
| `status [performance\|network\|memory\|worlds\|storage\|all]` | TPS, tick time, task pool threads, chunks |
| `stop` | saves the world and exits with code 0 |

Chorus's own `tp` and `gamemode` act only on the player who sends them, and it has no `effect`, `kill`, `time` or
`querytarget`.

## Configuration changes

[chorus.toml](chorus.toml) is the template of the run's `chorus.toml`. It holds only the keys the harness sets, each
with a one-line reason, and run.json records every key it writes. Every other key keeps the build's default as printed
by `chorusbench -defaults`. Chorus refuses unknown keys.

- Listen on `127.0.0.1` at the machine profile's port, so the bots connect over loopback and the newly built binary
  never triggers a firewall prompt.
- Offline login.
- The same view distance (`level.max_view_distance`, 32 by default, which also caps generation), max players, level
  name, seed and autosave setting as every server.
- `server.threads` is the number of server CPUs: `auto` counts every CPU of the machine on Windows, not the server's
  CPU set.
- The RakNet transport and `level.chunk_saving = "modified"` are written as their defaults and read back.

## Caveats

- Difficulty is always peaceful, game mode survival and the simulation distance 0 (no random block ticks), whatever
  the scenario asks; `spread100` asks for easy and 4. Chorus has no setting for them.
- It generates terrain using a port of the Java Edition overworld generator.
  Modern Java and Bedrock terrain are broadly aligned for the same seed, but feature placement and spawn logic differ, and Chorus does not yet implement Bedrock-specific decorators or structures.
- With `level.chunk_saving = modified`, generated chunks nobody changed are never written: the world folder stays a few
  kilobytes, and autosave and shutdown save nothing during a spread run.
- It sends no movement corrections and no locator-bar packets, and bots teleported to the spread height keep hovering
  there. `corrections_per_bot_s`, `locator_pkts_per_bot_s` and `airborne_frac_steady` check this in every run.
- There are no mobs, no damage and no effects, so placement sends no effect commands.
- Only the overworld exists, so every position reports dimension 0.
- With fewer than 10 threads the IO task pool gets none (`status performance` shows `0 io` at 4 threads), and storage
  runs on the async compute pool.
- When ticks run long, Chorus runs at most five ticks in a row to catch up and drops the rest, so the tick counter falls
  behind wall time instead of bursting.
- Chorus logs nothing when a player leaves or is kicked.
- Upstream changes daily and its git dependencies follow `main`, so a new commit usually needs a new `Cargo.lock` and
  changes to `main.rs`.

## Crash files

Chorus writes no crash files. A Rust panic prints its message to the console. When the server dies, exits nonzero or
has to be killed at stop, the harness keeps the last log lines in `crash/server-log-tail.txt` and copies Chorus's log
file of the run from `logs/`.

chorusbench is Apache-2.0, like the rest of the repository, and links Chorus (Apache-2.0); see
[NOTICE](../../NOTICE).
