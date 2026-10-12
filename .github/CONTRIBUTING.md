# Contributing to bedrock-bench

bedrock-bench is only useful if people can check its numbers. Most of the rules below keep every server's setup the
same and every result easy to audit.

## Development

```shell
uv sync                              # Python 3.11+ and the dev tools (pytest, ruff, jsonschema)
uv run pytest                        # unit, adapter contract and fake-server integration tests
uv run ruff check
uv run ruff format --check
uv run bbench docs metrics --check   # docs/metrics.md matches the metric registry
```

The tests never start a real Minecraft server and never download one. Two groups skip when a tool is missing: the
Java helper builds need a JDK (`javac` on PATH), and the figure script's test needs matplotlib, which CI adds with
`uv run --with matplotlib==3.11.2 pytest tests/unit/test_plot_results.py`. `tests/integration/test_contamination.py`
needs an idle machine. When the desktop is busy, rerun it alone.

Go code is vetted and tested per module with `GOWORK=off`, so each module builds from its own `go.mod` and `go.sum`,
as the release does:

```sh
for dir in bots/core bots/loadbot servers/dragonfly; do
  (cd "$dir" && GOWORK=off go vet ./... && GOWORK=off go test ./...)
done
gofmt -l bots servers/dragonfly   # prints nothing when every file is formatted
```

`bots/legacy1001` builds only once its gophertunnel fork tag is published
([bots/README.md](../bots/README.md#patches)).

To run the benchmark rather than work on the harness, see [docs/get_started.md](../docs/get_started.md).

Rules for code:

- **Data in files, behaviour in code.** The servers are the folders under `servers/`, their versions and pins are in
  each `server.toml`, and the workload is in `configs/`. Python never lists server names, versions, pins or workload
  values. The report layout is the one thing fixed in code.
- **Only adapters know server names.** Phases, samplers, metrics and the report never branch on a server name. A
  server that needs different behaviour gets an adapter attribute or method, which the harness uses the same way for
  every server.
- **Servers are black boxes.** Adapters read console output and what the operating system reports about the process.
  No hooks, and no injected code beyond the helpers in `servers/<id>/`.
- **Runtime dependencies** are the Python standard library, `psutil` and `jinja2`.
- **Metrics are pure functions** of a run's raw files, so `bbench analyze` recomputes them byte for byte.
- Python follows ruff (line length 120). Go follows gofmt and go vet. Comments are short.

## Content rules

- Nothing from Mojang: no BDS binaries, worlds or resource packs, in git, CI artifacts or release assets.
  `bbench fetch` downloads BDS on the user's machine after they accept the Minecraft EULA.
- No server binaries or worlds of any kind. Helpers are committed as source and built locally.
- No config files copied from a server. A template holds only the keys the harness sets.
- No results or benchmark figures, except in `docs/blog/`.
- No absolute paths, hostnames or usernames in code, fixtures or attached files.
- No decompiled code, reverse-engineering notes, leaked material or anything under an NDA, for any server. A caveat
  on a server page describes what anyone can observe, or links the server's public source or documentation.
- Every code file (Python, Go, Rust, shell, PHP, Java, workflow YAML) starts with `SPDX-License-Identifier: Apache-2.0`.
- Text files are UTF-8 with LF line endings.

## Pull requests

- One change per pull request. A refactor and a behaviour change go in separate pull requests.
- The title says what changed for someone who runs the harness or reads its results. GitHub builds the release notes
  from these titles.
- Say how you tested. A change that affects measurements includes `metrics.json` from before and after, computed from
  the same raw run with `bbench analyze`.

Checklist:

- [ ] `uv run pytest`, `uv run ruff check` and `uv run ruff format --check` pass.
- [ ] A metric, schema or config change follows [Versioning](#versioning).
- [ ] A new or updated server follows [Adding a server](#adding-a-server).
- [ ] The [content rules](#content-rules) hold.

## Adding a server

A server is one folder and one adapter module. The harness finds both by itself, and the contract test fails when a
folder has no adapter or an adapter has no folder.

| File | Holds |
|---|---|
| `servers/<id>/server.toml` | The pinned downloads, and the version a default session runs. |
| `servers/<id>/README.md` | The server page. |
| `servers/<id>/<config file>` | One template per config file the harness writes, named like the real file. |
| `servers/<id>/<helper sources>` | Helper plugin or program sources, flat next to the templates. No binaries, no worlds. |
| `bedrock_bench/adapters/<id>.py` | The adapter: how to fetch, prepare, launch and talk to the server. |
| `tests/fixtures/logs/<id>.toml`, `tests/fixtures/logs/<id>-session.log` | Hand-written console lines. |
| `tests/unit/test_adapter_<id>.py` | Unit tests. |

`<id>` is a short lowercase name, the same everywhere. It is the first half of the server's tags, `<id>@<version>`.

### Before you start

Find out from the server's documentation or source:

- **Protocol.** The bots speak protocol 2193 (`loadbot`) and 1001 (`legacy1001`). Another protocol needs a bot first
  ([bots/README.md](../bots/README.md#protocol-1001)).
- **Offline login.** The bots log in without Xbox authentication, so the server needs a switch for that.
- **A tick counter.** A console command that prints a counter that advances once per executed tick
  ([rule R8](../docs/methodology.md#rules)). World time and day time do not qualify. Without one, a helper adds it.
- **Positions.** A command that prints every player's position, from the server or a helper.
- **Two console lines:** one when the server is ready, and one when a player is in the world.

### Copy the closest real adapter

| The server is | Copy |
|---|---|
| a loader or mod on top of BDS | `endstone.py` or `levilamina.py` |
| a jar on the JVM | `powernukkitx.py`, or `nukkit.py` when it needs a helper plugin |
| a release file run by another runtime | `pocketmine.py` |
| a Go module built from source | `dragonfly.py` |
| a Rust crate built from source | `chorus.py` |
| a fork of a supported server | subclass its adapter, as `altay.py` does |

A new version of a supported server is a pin in its `server.toml`, not a new adapter. A new BDS version is a pin in
`servers/bds/server.toml`, which the Endstone and LeviLamina pins name with `bds`. A runtime that
`servers/runtimes.toml` lacks gets a pin there, in the same `[artifacts]` shape.

### server.toml

Every server file has the same shape: `schema`, `server` and two tables, nothing else.

```toml
# SPDX-License-Identifier: Apache-2.0

schema = 1
server = "<id>"

# What `bbench run` measures when it is given no tag.
[benchmark]
version = "1.4.2"

# Releases from GitHub, checked against the sha256 below.
[artifacts]
source = "github"                  # mojang, pypi, github, maven, web, build or local

[artifacts.version."1.4.2"]
protocol = 2193                    # picks the bot
runtime = "jdk-21.0.12.1"          # plain keys are for the adapter: runtime, commit, bds, python, ...

[artifacts.version."1.4.2".any]    # or .windows and .linux
url = "https://github.com/example/server/releases/download/1.4.2/server.jar"
sha256 = "<sha256 of the file>"
size = 0                           # bytes
```

- **`[benchmark]`.** `version` is the tag a default session runs. Endstone also sets the `bds` build it runs on and
  its `python`. `include = false` with a one-line `reason` leaves the server out of a default session. Keep it for
  real blockers, such as the bots failing to decode what the server sends.
- **`[artifacts]`.** One table per version with `protocol` and whatever else the adapter reads, and one table per
  platform (`windows`, `linux`, or `any` for a jar or a phar) with `url`, `sha256` and `size`, plus `executables` for
  files that need the exec bit and `eula = "minecraft"` for a download behind the Minecraft EULA. Pin builds anyone
  can download. A local build goes through `bbench smoke --artifact <path>`, and its runs are flagged
  `unpublished_artifact`.

Put a short comment above each table saying where its values come from.

### Config templates

Every config file the harness writes comes from a template in `servers/<id>/` with the same name and format as the
real file, such as `server.properties`, `pocketmine.yml`, `config.toml` or `LeviLamina.Config.json`. There is no
`.j2` suffix. The adapter names the templates it writes.

- A template lists only the keys the harness sets, each with a one-line comment above it saying why. JSON has no
  comments, so give those reasons on the server page.
- Values come from the config and the run, for example `{{ workload.server.view_distance }}`,
  `{{ workload.world.seed }}` and `{{ run.port }}`. An undefined name is an error.
- The adapter renders each template with Jinja and merges the result onto the file in the server directory. In a
  properties file the keys are set or added; YAML, TOML and JSON are deep merged. A file that does not exist yet is
  written as rendered, and the server fills in the rest.
- `run.json` records every key's value before and after, and the sha256 of each written file. After the run every
  key is read back, and a value the server changed makes the run invalid (`config_readback`).
- A BDS-hosted server keeps its own copy of `server.properties`, identical to `servers/bds/server.properties`. The
  contract test checks this.

Change only what the benchmark needs:

- what it takes to drive the server: offline login, the port, the console, the helper, and raised per-address limits,
  since every bot connects from 127.0.0.1;
- the workload: view distance, simulation distance, seed, difficulty, game mode, autosave, player slots and
  client-side generation off;
- telemetry, crash upload and update checks, turned off wherever the server has a switch;
- a default the server cannot run the workload with. Its comment says what the default does, as
  `memory.main-hard-limit` in `servers/pocketmine/pocketmine.yml` does.

Change nothing else. A server that crashes as configured is reported as crashed.

### Helpers

A helper adds only what the console lacks: a tick counter, a positions command, or a raised per-address limit the
server has no config key for. It never changes the simulation. Its sources sit flat in `servers/<id>/` next to the
templates, and the adapter builds or places them at fetch or prepare time. `servers/dragonfly/` keeps its Go module
layout and `testdata/`.

### The adapter

Class attributes are the contract. `ServerAdapter.class_problems()` checks them when the registry loads the module.

- `name` (the `<id>`), `platforms`, `bot_compat_flags` (only `-fake-xuid` is allowed) and `supports_csg`. The
  protocol, and with it the bot, comes from `protocol` in `server.toml`.
- `dialect`. `prefix` is stripped from every line before any other pattern runs. Name its groups `level` and
  `logger`. `level_map` maps raw levels onto `debug`, `info`, `warn`, `error` and `fatal`, and `benign` lists
  error-level lines that are not errors. `version` must match the tag's version. `player_in_world` gates the effects
  and the teleport, so if the server prints a "connected" line first, pick the later one. `tick_cmd` and `tick_re` are
  the tick probe. Console input is written with LF. Set `newline` only if the server needs CRLF.
- `tick_thread_rule`: `name:<thread>`, `lowest_tid_name:<thread>`, `java_main`, `first_created` or `none`, using
  thread names seen at runtime (the thread list in `run.json`).
- `crash_paths`: files or folders to check when the server dies or exits with an error. Files in them that changed
  during the run are copied into the run's `crash/` folder.

Methods:

- `fetch()` downloads through the pins, checks size and sha256, and installs read-only into the cache. It must be
  safe to run twice.
- `prepare()` copies the install into a fresh server directory, writes each config with
  `self.write_config(server_dir, "<file>", ctx)` and returns the layout with the written files. Never edit the
  install.
- `launch()` returns the command line and changes nothing. Answers to first-start prompts go in
  `LaunchSpec.stdin_lines`, environment changes in `env_set` and `env_remove`. `server_process()` picks the measured
  process when a launcher starts the server as a child.
- `cmd_effects()` and `cmd_tp()` default to vanilla syntax. `parse_positions()` reads the reply to `cmd_positions()`
  and returns None, never an empty list, when the reply is missing. A helper that prints `positions: N` and then
  `position: <name> <x> <y> <z> <dim>` lines can use `standalone.parse_helper_positions`.
- `facts()` returns what `run.json` records about the process, such as the allocator and JVM flags.

### The server page

`servers/<id>/README.md` opens with one paragraph: language, project link and licence. Then come these sections:

- **Setup:** what fetch does, and any licence gate.
- **How a run starts:** the process tree, the measured process, where the tick probe comes from, and the positions
  command.
- **Configuration changes:** a short plain list of what the templates change, with links to them.
- **Caveats:** observable behaviour only.
- **Crash files.**

### Tests and smoke

- `tests/fixtures/logs/<id>.toml` holds one `[[sample]]` per dialect role (see `bds.toml` for the roles), and
  `<id>-session.log` holds a whole made-up session. Write them by hand in the server's line format.
- `tests/unit/test_adapter_<id>.py` checks what the contract test cannot, such as the rendered configs and position
  parsing.
- The contract test must pass:

  ```shell
  uv run pytest tests/unit/test_adapters_contract.py -k <id>
  ```

  It checks the class attributes, the server folder, that every pinned version speaks a bot's protocol, the dialect
  against the fixtures, that commands are single lines, the bot flag allowlist and that `parse_positions([])` returns
  None.
- Smoke on every platform the adapter declares, and attach the smoke's `run.json` to the pull request:

  ```shell
  uv run bbench bots build
  uv run bbench fetch <id>@<version>
  uv run bbench smoke configs/smoke.toml <id>@<version>
  ```

  The smoke runs 4 bots with short phases. It passes when the run is `ok` and the server answers no harness command
  with an error line.

Once `[benchmark]` names a version, a default `bbench run` includes the server. Add it to the table in
[README.md](../README.md#supported-servers). If a helper is built against the server's code, say so in `NOTICE`, as
for Dragonfly and Chorus.

## Versioning

These versions decide whether two results can be compared. Every run records them.

- **Harness.** `__version__` in `bedrock_bench/__init__.py`, following Semantic Versioning. A release is tagged
  `v<version>`.
- **Analysis.** `ANALYSIS_VERSION` in the same file. Any change to a metric formula bumps it. Then regenerate the
  fixture runs with `uv run python tests/fixtures/runs/synthetic.py` and check the diff, and regenerate
  `docs/metrics.md` with `uv run bbench docs metrics`. `bbench analyze` brings older runs to the new version from
  their raw files.
- **Result schemas.** `bedrock-bench/run/<major>` and `bedrock-bench/metrics/<major>` in `bedrock_bench/schemas/`.
  - An additive change (a new optional field or enum value) bumps `SCHEMA_MINOR` and updates the schema and the
    fixtures `tests/fixtures/run.ok.json` and `tests/fixtures/metrics.json`.
  - Removing, renaming or redefining a field bumps `SCHEMA_VERSION`. Keep the old schema file next to the new one and
    add a migration in `results.py`.
- **Bot events.** Every event carries `v`, which is `EventVersion` in `bots/core/events.go` and a constant in
  `bot-event.schema.json`. New fields are allowed without a bump. Any other change bumps `v` in both places.
- **Configs.** Every run records its config's name, the configs it inherits from, the resolved values and their
  hash. A changed value in `configs/spread.toml` changes the hash of every config built on it, so old and new runs no
  longer compare. To try another setup, add a config with `base = "spread"` rather than edit one that has results.

Runs with different configs, machine profiles or machines never share a report.

## Profiling

bedrock-bench measures. It does not profile. Profilers, thread dumps and traces change what they observe: sampling
slows the tick loop, trace files get scanned by antivirus software during the run, and a Java thread dump pauses the
JVM. Profile in a separate run, and never report its numbers as results.

Produce the load with the harness, on the machine of the run you want to explain, and attach the profiler from
outside. `run-one` is the command `bbench run` starts for each run:

```shell
uv run bbench run-one configs/spread-100.toml --server bds@1.26.52.3 --out runs/profiled
```

- Watch `run.partial.json` for the marks `alljoined`, `settled` and `steady0` to know when to start and stop. Many
  profilers take a few seconds to produce samples, so start early and discard the first seconds.
- The run usually ends `invalid`, because the profiler counts as background load. That is expected.
- Write profiler output outside the run directory, ideally on a disk antivirus does not scan.
- Pin the profiler to the bot CPUs, or run it after the server has stopped.
- For tick-rate questions, filter to the thread the adapter's `tick_thread_rule` picks. The run's
  `series/threads.csv.gz` lists every thread by id and name. Profile the view load and the steady state in separate
  windows.
- Compare milliseconds per tick for an item, not its share of the profile. A faster server does more work per second.
- When you report findings from a profile, say so, and give the run id of the unprofiled run they explain.

Reading server internals, or anything that needs non-public material, is out of scope for this repository.

## Releasing

1. Set `__version__` in `bedrock_bench/__init__.py` to the release version `X.Y.Z` and commit. `bbench bots fetch`
   downloads from the release tagged `v<__version__>`.
2. Run the checks below.
3. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`. The Release workflow builds the bots on Linux and
   Windows, checks that both builds are identical, and attaches the bots for linux/amd64 and windows/amd64 with
   `SHA256SUMS`, `LICENSE` and `NOTICE`. GitHub generates the release notes. A tag with a hyphen becomes a
   pre-release.
4. On a clean checkout of the tag, check that `uv run bbench bots fetch` downloads and verifies the bots.
5. Set `__version__` to the next `.dev0` version.

Before tagging:

- [ ] CI is green on `main`: lint, generated files, tests on Ubuntu and Windows, the Go checks, the identical bot
      builds and the Dragonfly smoke.
- [ ] `uv run bbench smoke configs/smoke.toml <id>@<version>` passes for every version pinned in `servers/`, on each
      platform its adapter declares. CI runs only the Dragonfly smoke. A server that a default `bbench run` skips is
      exempt.
- [ ] The decode smoke passes. CI never downloads BDS, so this runs by hand: against every pinned server except the
      exempt ones above, run the bot its adapter uses (`legacy1001` for PocketMine-MP and Allay, `loadbot` for the
      others) for at least five minutes of walking and teleports:

      ```sh
      loadbot -addr 127.0.0.1:<port> -n 4 -decode-errors fail
      ```

      Exit code 3 is a reproduction and goes through the [patch policy](../bots/README.md#policy). Any other exit
      code means no patch is needed. Attach the results to the release.

## Reporting a problem

Open an issue and attach:

- `run.json` and `metrics.json` of the run, or `report.md` and `summary.json` of the session;
- the output of `bbench doctor --json`, and the machine profile if you passed `--machine`;
- the run's `crash/` folder if the server crashed.

`run.json` holds no hostname, username or absolute path. Raw logs are not scrubbed, so read any log before you
attach it.
