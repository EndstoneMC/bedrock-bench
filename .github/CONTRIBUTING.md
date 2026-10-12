# Contributing to bedrock-bench

bedrock-bench is only useful if people trust its numbers. Most of the rules below keep results comparable and
auditable.

## Development

```shell
uv sync                              # Python 3.11+ and the dev tools (pytest, ruff, jsonschema)
uv run pytest                        # unit, adapter contract and fake-server integration tests
uv run ruff check
uv run ruff format --check
uv run bbench docs metrics --check   # docs/metrics.md matches the metric registry
```

The tests never start a real Minecraft server and never download one. `tests/integration/test_contamination.py`
needs an idle machine. When the desktop is busy, rerun it alone.

Go code is vetted and tested per module with `GOWORK=off`, so each module builds from its own `go.mod` and `go.sum`
as the release does (Go 1.25+):

```sh
for dir in bots/core bots/loadbot bots/legacy1001 servers/dragonfly; do
  (cd "$dir" && GOWORK=off go vet ./... && GOWORK=off go test ./...)
done
gofmt -l bots servers/dragonfly   # prints nothing when every file is formatted
```

To run benchmarks rather than develop the harness, see [docs/get_started.md](../docs/get_started.md).

Rules for code:

- **Only adapters know server names.** Phases, samplers, metrics and the report never branch on a server name. A
  server that needs different behaviour gets an adapter attribute or method, which the harness uses generically.
- **Servers are black boxes.** Adapters read console output and what the operating system reports about the process.
  No hooks, and no injected code beyond the helper plugins in `configs/`.
- **Runtime dependencies** are the Python standard library and `psutil`. Anything else needs a strong case.
- **Metrics are pure functions** of a run's raw files, so `bbench analyze` recomputes them byte for byte.
- Python follows ruff (line length 120). Go follows gofmt and go vet. Comments are short.

## Content rules

- Nothing from Mojang: no BDS binaries, worlds, world templates or resource packs, in git, CI artifacts or release
  assets. `bbench fetch` downloads BDS on the user's machine after they accept the Minecraft EULA.
- No server binaries or worlds of any kind. Helpers are committed as source and built locally.
- No absolute paths, hostnames or usernames in code, fixtures or attached files.
- No decompiled code, reverse-engineering notes, leaked material or anything under an NDA, for any server. Feature
  evidence is public: source at a pinned tag or commit, or official documentation.
- Every code file (Python, Go, shell, PHP, Java, workflow YAML) starts with `SPDX-License-Identifier: Apache-2.0`.
- Text files are UTF-8 with LF line endings.

## Pull requests

- One change per pull request. A refactor and a behaviour change go in separate pull requests.
- The title says what changed for someone who runs the harness or reads its results. GitHub builds the release notes
  from these titles.
- Say how you tested. A change that affects measurements includes `metrics.json` from before and after, computed from
  the same raw run with `bbench analyze`.

Checklist:

- [ ] `uv run pytest`, `uv run ruff check` and `uv run ruff format --check` pass.
- [ ] A metric, schema or scenario change follows [Versioning](#versioning).
- [ ] A new or updated server follows [Adding a server](#adding-a-server).
- [ ] The [content rules](#content-rules) hold.

## Adding a server

A server is one folder and one module. The harness finds the adapter by itself, and nothing outside the adapter may
test for the server's name.

| File | Holds |
|---|---|
| `bedrock_bench/adapters/<id>.py` | The adapter, mostly data. |
| `servers/<id>/server.toml` | Pinned downloads, declared feature parity, and the config values the harness writes. |
| `servers/<id>/README.md` | The server page. |
| `servers/<id>/<files>` | Helper plugin sources and config templates, flat. No binaries, no worlds. |
| `tests/fixtures/logs/<id>.toml`, `tests/fixtures/logs/<id>-session.log` | Hand-written console lines. |
| `tests/unit/test_adapter_<id>.py` | Unit tests. |

### Before you start

Find out from the server's documentation or source:

- **Protocol.** The bots speak protocol 2193 (`loadbot`) and 1001 (`legacy1001`, for PocketMine-MP and Allay). Another
  protocol needs a bot frontend first ([bots/README.md](../bots/README.md#protocol-1001)).
- **Offline login.** The bots log in without Xbox authentication, so the server needs a switch for that.
- **A tick counter.** A console command that prints a counter that advances once per executed tick
  ([rule R8](../docs/methodology.md#rules)). World time and day time do not qualify. Without one, a helper adds it.
- **Positions.** A command that prints every player's position, from the server or a helper. Bot-reported
  positions are a labelled fallback.
- **Two console lines:** one when the server is ready, and one when a player is in the world.

### Copy the closest real adapter

| The server is | Copy |
|---|---|
| a loader or mod on top of BDS | `endstone.py` or `levilamina.py` (`BdsFamily`) |
| a jar on the JVM | `powernukkitx.py`, or `nukkit.py` when it needs a helper plugin (`JavaServer`) |
| a release file run by another runtime | `pocketmine.py` (`StandaloneServer`) |
| a Go module built from source | `dragonfly.py` |
| a Rust crate built from source | `chorus.py` |
| a fork of a supported server | subclass its adapter, as `altay.py` does |

A new BDS version is a pin in `servers/bds/server.toml`, not an adapter. A runtime that
`servers/runtimes.toml` lacks gets a pin there, in the same `[artifacts]` shape.

### server.toml

Every server file has the same top level:

```toml
# SPDX-License-Identifier: Apache-2.0

schema = 1
server = "<id>"                        # optional, must match the folder name

[artifacts]
source = "github"                      # mojang, pypi, github, maven, web, build or local

[artifacts.version."1.4.2"]
protocol = 2193                        # network protocol of this version; picks the bot build
runtime = "jdk-21.0.12.1"              # standalone servers: a version in servers/runtimes.toml

[artifacts.version."1.4.2".any]        # or .windows and .linux
url = "https://github.com/example/server/releases/download/1.4.2/server.jar"
sha256 = "<sha256 of the file>"
size = 0

[features]

[features.range."1.4"]
fidelity = "V1"
"terrain.vanilla_generator" = { class = "absent", evidence = "https://github.com/example/server/blob/1.4.2/README.md", note = "own generator" }
"player.server_authoritative_movement" = { class = "absent", observed_by = "corrections_per_bot_s" }

[config]

[config.file."server.properties"]
format = "properties"                  # or yaml

[config.file."server.properties".harness]
server-port = { value = "{port}", default = "19132", reason = "machine profile port" }

[config.file."server.properties".parity]
view-distance = { value = "{view_distance}", default = "16", reason = "the same view distance on every server" }
```

- **`[artifacts]`.** One table per version and platform (`windows`, `linux`, or `any` for a jar or a phar) with `url`,
  `sha256` and `size`, plus `executables` for files that need the exec bit and `eula = "minecraft"` for a download
  behind the Minecraft EULA. Pin builds anyone can download. A local build goes through
  `--artifact <path>`, and its runs are flagged `unpublished_artifact`.
- **`[features]`.** One range per version prefix (the longest match wins) with `fidelity` and one cell per feature
  key. Cover at least every key a scenario `exercises`. Classes, fidelity levels and the evidence rule are in
  [docs/methodology.md](../docs/methodology.md#fairness-and-feature-parity).
- **`[config]`.** Every value the harness writes, file by file, in the table of its override category, with the
  server's default and a reason. Entries may also take `type`, `variants` and `skip_variants`. Placeholders in braces
  come from `config_context` in `bedrock_bench/adapters/standalone.py`. BDS-hosted servers have no `[config]` and use
  the one in `servers/bds/server.toml`. bds and dragonfly use a one-file form: `file = "..."` with
  `[config.<category>]` tables.

Put a short comment above each table saying where its values come from.

### The adapter

Class attributes are the contract. `ServerAdapter.class_problems()` checks them when the registry loads the module.

- `name` (the folder name), `platforms`, `bot_compat_flags` (only `-fake-xuid` is allowed) and `supports_csg`.
- `dialect`. `prefix` is stripped from every line before any other pattern runs. Name its groups `level` and
  `logger`. `level_map` maps raw levels onto `debug`, `info`, `warn`, `error` and `fatal`, and `benign` lists
  error-level lines that are not errors. `version` must match the arm version. `player_in_world` gates the effects and
  the teleport, so if the server prints a "connected" line first, pick the later one. `tick_cmd` and `tick_re` are the
  tick probe. Console input is written with LF. Set `newline` only if the server needs CRLF.
- `tick_thread_rule`: `name:<thread>`, `lowest_tid_name:<thread>`, `java_main`, `first_created` or `none`, using
  thread names seen at runtime (the thread census in `run.json`). `thread_groups` are diagnostic only.
- `crash_paths`: files or folders to check when the server dies or exits with an error. Files in them that changed
  during the run are copied into the run's `crash/` folder.
- `variants`: `{"<name>": "what it changes"}`. An arm may only use a declared variant, and its tag becomes
  `<id>@<version>+<variant>`.

Methods:

- `fetch()` downloads through the pins, verifies size and sha256, and installs read-only into the cache. It must be
  idempotent.
- `prepare()` copies the install into the run's server directory, writes the configs and returns every value that
  differs from the server's default as an `Override` ([rule R6](../docs/methodology.md#rules)). Never edit the
  install.
- `launch()` is pure. Answers to first-start prompts go in `LaunchSpec.stdin_lines`, environment changes in `env_set`
  and `env_remove`. `server_process()` picks the measured process when a launcher starts the server as a child.
- `cmd_effects()` and `cmd_tp()` default to vanilla syntax. `parse_positions()` reads the reply to `cmd_positions()`
  and returns None, never an empty list, when the reply is missing. A helper that prints `positions: N` and then
  `position: <name> <x> <y> <z> <dim>` lines can use `standalone.parse_helper_positions`.
- `facts()` returns what `run.json` records about the process: allocator, JVM flags, the effective client-side
  generation setting.

### Config changes

Every config value that differs from the server's default is an override with a category:

- `harness`: needed to drive the server at all, such as offline login, ports, console, helper plugin and raised
  per-IP limits. Telemetry, crash upload and auto-update are turned off here wherever the server has a switch.
- `parity`: aligns the server with the scenario, such as distances, seed, difficulty and heap.
- `rescue`: the default config fails the workload. The reason says what the default does, and the report prints it
  as a footnote.
- `nonvanilla`: changes gameplay to save work. Always footnoted, usually behind a variant.

Change nothing else. Write files as UTF-8 with LF line endings.

### Helpers

A helper plugin adds only what the console lacks: a tick counter, a positions command, or a raised per-IP packet
limit the server has no config key for. It never changes the simulation. Its sources sit flat in
`servers/<id>/`, and the adapter builds or places them at fetch or prepare time. Config templates the harness
starts from sit next to them.

### The server page

`servers/<id>/README.md` opens with one paragraph: language, project link and license. Then come these
sections:

- **Setup:** what fetch does, and any license gate.
- **How a run starts:** the process tree, the measured process, where the tick probe comes from, and the positions
  command.
- **Configuration changes:** a short list that links `server.toml` for the values and reasons.
- **Variants.**
- **Caveats:** observable behaviour only.
- **Crash files.**

### Tests and smoke

- `tests/fixtures/logs/<id>.toml` holds one `[[sample]]` per dialect role (see `bds.toml` for the roles), and
  `<id>-session.log` holds a whole synthetic session. Write them by hand in the server's line shape.
- `tests/unit/test_adapter_<id>.py` checks what the contract test can't, such as the rendered configs and their
  overrides, and position parsing.
- The contract test must pass:

  ```shell
  uv run pytest tests/unit/test_adapters_contract.py -k <id>
  ```

  It checks the class attributes, the dialect against the fixtures, that commands are single lines, the bot flag
  allowlist, `parse_positions([])` returning None, and that the feature ranges of the pinned versions cover every
  exercised key. Run it once with `BBENCH_UPDATE_SNAPSHOTS=1` to write `tests/unit/snapshots/adapters/<id>.json`, the
  rendered commands later runs are compared with.

- Smoke on every platform the adapter declares, and attach the smoke's `run.json` to the pull request:

  ```shell
  uv run bbench fetch <id>@<version>
  uv run bbench smoke <id>@<version>
  ```

  The smoke runs 4 bots with short phases. It passes when the run is `ok`, the server accepts every command the
  harness sends, and the bots see movement corrections where the feature file
  declares server-authoritative movement.

To benchmark the server, add an arm to a matrix in `configs/matrices/`:

```toml
[[arm]]
server = "<id>@<version>"
variant = "<variant>"   # optional
```

A separate package can register adapters through the `bedrock_bench.adapters` entry point group. They follow the
same contract.

## Versioning

These versions decide whether two results can be compared. Every run records them.

- **Harness.** `__version__` in `bedrock_bench/__init__.py`, following Semantic Versioning. A release is tagged
  `v<version>`.
- **Analysis.** `ANALYSIS_VERSION` in the same file. Any change to a metric formula bumps it. Then regenerate the
  golden runs with `uv run python tests/fixtures/runs/synthetic.py` and check the diff, and regenerate
  `docs/metrics.md` with `uv run bbench docs metrics`. `bbench analyze` brings older runs to the new version from
  their raw files. A metric id ends in its unit: `_s`, `_ms`, `_mib`, `_cores`, `_ms_per_s`, `_frac`, `_per_bot_s`.
- **Result schemas.** `bedrock-bench/run/<major>` and `bedrock-bench/metrics/<major>` in `bedrock_bench/schemas/`.
  - An additive change (a new optional field or enum value) bumps `SCHEMA_MINOR`, and updates the schema and the
    fixtures `tests/fixtures/run.ok.json` and `tests/fixtures/metrics.json`.
  - Removing, renaming or redefining a field bumps `SCHEMA_VERSION`. Keep the old schema file next to the new one and
    add a migration in `results.py`.
- **Bot events.** Every event carries `v`, which is `EventVersion` in `bots/core/events.go` and a constant in
  `bot-event.schema.json`. New fields are allowed without a bump. Any other change bumps `v` in both places.
- **Scenarios.** Every run stores the hash of its scenario's resolved content, so comments don't change it. A changed
  scenario makes old and new runs incomparable, and `bbench run` refuses to add runs to a matrix directory that holds
  the old version. Add a new scenario file rather than change one that has results.

Runs with different scenario hashes, analysis versions or machines never share a table
([docs/methodology.md](../docs/methodology.md#statistics-and-run-status)).

## Profiling

bedrock-bench measures. It does not profile. Profilers, thread dumps and traces change what they observe: sampling
slows the tick loop, trace files make antivirus software scan them during the run, and a Java thread dump pauses the
JVM. Profile in a separate run, and never report its numbers as results
([docs/methodology.md](../docs/methodology.md#publishing)).

Produce the load with the harness, on the machine and scenario of the run you want to explain, and attach the
profiler from outside:

```shell
uv run bbench run-one --server bds@1.26.52.3 --scenario spread100 --out runs/profiled
```

- Watch `run.partial.json` for the marks `alljoined`, `settled` and `steady0` to know when to start and stop. Many
  collectors take a few seconds to produce samples, so start early and discard the first seconds.
- The run usually ends `invalid`, because the profiler counts as foreign load. That is expected.
- Write profiler output outside the run directory, ideally on a disk antivirus does not scan, and decode traces after
  the run.
- Pin the profiler to the bot CPUs, or run it after the server has stopped.
- For tick-rate questions, filter to the thread the adapter's `tick_thread_rule` picks. The run's `threads.csv.gz`
  lists every thread by id and name. Profile the join and the steady state in separate windows.
- Compare milliseconds per tick for an item, not its share of the profile. A faster server does more work per second.
- When you report findings from a profile, say so, and give the run id of the unprofiled run they explain.

Reading server internals, or anything that needs non-public material, is out of scope for this repository.

## Releasing

1. Set `__version__` in `bedrock_bench/__init__.py` to the release version `X.Y.Z` and commit. `bbench bots fetch`
   downloads from the release tagged `v<__version__>`.
2. Run the checks below.
3. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`. The Release workflow builds the bots on Linux and
   Windows, checks that both builds are identical, and attaches `loadbot` and `legacy1001` for linux/amd64 and
   windows/amd64 with `SHA256SUMS`, `LICENSE` and `NOTICE`. GitHub generates the release notes. A tag with a hyphen
   becomes a pre-release.
4. On a clean checkout of the tag, check that `uv run bbench bots fetch` downloads and verifies the bots.
5. Set `__version__` to the next `.dev0` version.

Before tagging:

- [ ] CI is green on `main`: lint, tests on Ubuntu and Windows, the Go checks and the Dragonfly smoke.
- [ ] `bbench smoke <id>@<version>` passes for every version pinned in `configs/`, on each platform its
      adapter declares. CI runs only the Dragonfly smoke. A pin its server README marks as invalid is exempt:
      `chorus@95ca835` ([caveats](../servers/chorus/README.md#caveats)).
- [ ] The decode smoke passes. CI never downloads BDS, so this runs by hand: against every pinned server except the
      exempt pins above, run the bot its adapter uses (`legacy1001` for PocketMine-MP and Allay, `loadbot` for the
      others) for at least five minutes of walking and teleports:

      ```sh
      loadbot -addr 127.0.0.1:<port> -n 4 -decode-errors fail
      ```

      Exit code 3 is a reproduction and goes through the [patch policy](../bots/README.md#patches). Any other exit
      code means no patch is needed. Attach the results to the release.

## Reporting a problem

Open an issue and attach:

- `run.json` and `metrics.json` of the run, or `summary.json` and `report.md` for a matrix;
- the output of `bbench doctor --json`, and the machine profile if you passed `--machine`;
- the run's `crash/` folder if the server crashed.

`run.json` holds no hostname, username or absolute path. Raw logs are not scrubbed, so read any log before you
attach it.
