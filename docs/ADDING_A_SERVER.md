# Adding a server

This guide adds a server called **Chorus**. Chorus stands in for any Bedrock server: its console lines, commands and
config keys below are made up to show the shape of the work. Replace them with what your server actually prints and
accepts.

A server needs:

| File | Purpose |
|---|---|
| `src/bedrock_bench/adapters/chorus.py` | the adapter: 60 to 150 lines, mostly data |
| `servers/chorus/` | config templates and helper plugin sources (no binaries, no worlds) |
| `artifacts/chorus.toml` | pinned downloads with sha256 |
| `features/chorus.toml` | declared feature parity with evidence |
| `docs/servers/chorus.md` | versions, setup, every override with its reason, default-config failure modes |
| `tests/fixtures/logs/chorus*` and `tests/unit/test_adapter_chorus.py` | line-shape fixtures and unit tests |

Adapters are discovered automatically. Nothing else in the harness changes, and nothing outside the adapter may test
for the server's name.

## 1. Check the prerequisites

Before writing code, find out from the server's documentation or source:

- **Protocol.** The harness bot speaks protocol 2193 (`loadbot`) and, for PocketMine-MP only, 1001 (`legacy1001`).
  A server on another protocol needs a bot frontend first ([bots/README.md](../bots/README.md)).
- **Offline login.** The bots log in without Xbox authentication. The server must allow that (an `online-mode` or
  `xbox-auth` switch).
- **A tick counter.** TPS needs a console command that prints a counter advancing exactly once per executed tick
  (rule R8). World time or day time is not enough: it can stop, jump or be scaled. If the server has no such command,
  write a small helper plugin that adds one and put its source in `servers/chorus/`.
- **Positions.** The spread check needs every player's position. Use a server command if one exists, otherwise add one
  to the helper. Bot-reported positions are a labelled fallback.
- **A ready line and a "player is in the world" line** in the console output.

## 2. Pin the download

`artifacts/chorus.toml`:

```toml
server = "chorus"
source = "github"

[version."1.4.2".windows]
url = "https://github.com/example/chorus/releases/download/v1.4.2/chorus-1.4.2-windows-x64.zip"
sha256 = "<sha256 of the file>"
size = 0                      # bytes; filled in on the first verified fetch
executables = []

[version."1.4.2".linux]
url = "https://github.com/example/chorus/releases/download/v1.4.2/chorus-1.4.2-linux-x64.zip"
sha256 = "<sha256 of the file>"
executables = ["chorus"]      # get the exec bit after extraction
```

Use a platform key `any` for a platform-independent file (a jar or a phar). Pin released builds only; a local build can
be tested with `--artifact <path>`, which marks the results `unpublished_artifact`.

## 3. Write the adapter

`src/bedrock_bench/adapters/chorus.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Chorus: a Bedrock server written in <language>."""

from __future__ import annotations

import hashlib
import re

from bedrock_bench import config, fetch
from bedrock_bench.adapters.base import ChaosDialect, Dialect, ServerAdapter, host_os
from bedrock_bench.model import (
    ConfigFile,
    Install,
    LaunchSpec,
    Layout,
    LogLine,
    Override,
    OverrideCategory,
    PlayerPos,
    WorldInfo,
)

H, P = OverrideCategory.HARNESS, OverrideCategory.PARITY

# key -> (Chorus default, category, reason); values come from the scenario or the machine profile
KEYS = {
    "online-mode": ("true", H, "offline bots"),
    "port": ("19132", H, "machine profile port"),
    "view-distance": ("12", P, "scenario view distance"),
    "simulation-distance": ("6", P, "scenario simulation distance"),
    "seed": ("", P, "scenario seed"),
    "level-name": ("world", P, "scenario level name"),
    "difficulty": ("normal", P, "scenario difficulty"),
    "max-players": ("20", P, "scenario max players"),
}


class ChorusAdapter(ServerAdapter):
    name = "chorus"
    platforms = frozenset({"windows", "linux", "docker"})
    protocol = 2193
    supports_csg = False
    tick_thread_rule = "name:chorus-tick"
    thread_groups = {"chunk workers": r"^chorus-chunk-\d+$"}
    crash_paths = ("crash-reports",)
    dialect = Dialect(
        ready=re.compile(r"Chorus is ready"),
        prefix=re.compile(r"^\d\d:\d\d:\d\d (?P<level>[A-Z]+) \[(?P<logger>[^\]]+)\] "),
        level_map={"DEBUG": "debug", "INFO": "info", "WARN": "warn", "ERROR": "error", "FATAL": "fatal"},
        benign=(),
        version=re.compile(r"Starting Chorus (?P<version>\d+\.\d+\.\d+)"),
        player_in_world=re.compile(r"(?P<player>\S+) entered the world"),
        tick_cmd="tick",
        tick_re=re.compile(r"Current tick: (?P<tick>\d+)"),
    )

    def fetch(self, cache):
        platform = "linux" if self.machine.runtime == "docker" else host_os()
        executables: tuple[str, ...] = ()
        if self.arm.artifact is not None:  # local build: results are marked unpublished_artifact
            archive, ref = fetch.adopt_local(self.arm.artifact, cache)
        else:
            spec = config.load_artifacts(self.name).get(self.arm.version, platform)
            archive, ref = fetch.obtain(spec, cache)  # downloads once, verifies size and sha256
            executables = spec.executables
        root = cache.install_dir(self.name, self.arm.version, platform, ref.sha256)
        tree = fetch.ensure_install(cache, root, lambda tmp: fetch.extract_zip(archive, tmp, executables=executables))
        return Install(root=root, tree_sha256=tree, artifacts=(ref,))

    def prepare(self, install, ctx):
        server_dir = ctx.work_dir / "chorus"
        fetch.copy_install(install.root, server_dir)  # writable copy of the read-only install
        s, w = ctx.scenario.server, ctx.scenario.world
        values = {
            "online-mode": "false",
            "port": str(self.machine.port),
            "view-distance": str(s.view_distance),
            "simulation-distance": str(s.tick_distance),
            "seed": str(w.seed),
            "level-name": w.level_name,
            "difficulty": s.difficulty,
            "max-players": str(s.max_players),
        }
        text = "".join(f"{key}={value}\n" for key, value in values.items())
        (server_dir / "chorus.properties").write_text(text, encoding="utf-8", newline="\n")
        overrides = tuple(
            Override(category, "chorus.properties", key, default, values[key], reason)
            for key, (default, category, reason) in KEYS.items()
            if values[key] != default
        )
        written = ConfigFile("chorus.properties", hashlib.sha256(text.encode()).hexdigest())
        world = WorldInfo(fresh=True, generator="custom", path=server_dir / "worlds" / w.level_name)
        return Layout(server_dir=server_dir, overrides=overrides, config_files=(written,), world=world)

    def launch(self, layout):
        exe = "chorus.exe" if self.machine.runtime == "native" and host_os() == "windows" else "chorus"
        return LaunchSpec(argv=(str(layout.server_dir / exe), "--no-gui"), cwd=layout.server_dir)

    def cmd_positions(self):
        return "positions"

    def parse_positions(self, lines: list[LogLine]):
        found = []
        for line in lines:
            m = re.search(r"(?P<name>\S+) at (?P<x>-?[\d.]+) (?P<y>-?[\d.]+) (?P<z>-?[\d.]+) in (?P<dim>\d)", line.text)
            if m:
                found.append(PlayerPos(m["name"], float(m["x"]), float(m["y"]), float(m["z"]), int(m["dim"])))
        return found or None  # None = unknown, which makes the spread invalid; never "0 players"

    def chaos_dialect(self):
        return ChaosDialect(
            renderers={
                "kill": lambda player, **_: f"kill {player}",
                "weather": lambda weather, **_: f"weather {weather}",
            },
            unsupported={"tickspeed": "Chorus has no random tick speed setting"},
        )

    def facts(self, layout, proc):
        return {"allocator": "default", "csg_setting": False}
```

Notes on each part:

- **Class attributes** are the contract. `ServerAdapter.class_problems()` checks them when the registry loads the
  module, and the contract test checks them again with your fixtures.
- **`dialect.prefix`** is stripped from every line before any other pattern runs, including lines of multi-line JSON
  output. Name the groups `level` and `logger` if the prefix carries them. `level_map` maps raw levels onto `debug`,
  `info`, `warn`, `error` and `fatal`; `benign` lists error-level lines that are not errors.
- **`dialect.version`** must match the banner version against the arm version (`version_match` gate).
- **`player_in_world`** gates effects and the teleport. If the server prints a "connected" line before the player is in
  the world, pick the later line.
- **`tick_thread_rule`** names the thread whose CPU becomes tick-thread CPU: `name:<thread>`,
  `lowest_tid_name:<thread>`, `java_main`, `first_created` or `none`. Use names observed at runtime (the thread census
  in `run.json`). `none` means only process CPU is reported, which is fine for servers without a single tick thread.
- **`thread_groups`** are diagnostic regexes for the thread table, never a headline.
- **`prepare()`** copies the read-only install into `ctx.work_dir`, renders configs and returns every value that differs
  from the server's default as an `Override` with a category (rule R6). Write files with LF line endings. Never edit the
  install.
- **`launch()`** is pure. Put answers to first-start prompts in `LaunchSpec.stdin_lines`, environment changes in
  `env_set` and `env_remove`.
- **Commands.** `cmd_effects` and `cmd_tp` default to vanilla syntax. Override them only if the server differs.
- **`chaos_dialect()`** renders chaos actions. Every action in `scenarios/chaos-actions.toml` must be rendered or listed
  in `unsupported` with a reason; see `adapters/bdsfamily.py` for the full set. Unsupported actions are counted, never
  sent.
- **`applied()`** recognises log lines that confirm an action took effect (for example a tick-speed change).
- **`facts()`** reports what the run should record about the process: allocator, JVM flags, the effective
  client-side generation setting.
- **Variants** (`variants = {"stock": "default memory limits"}`) select alternative override sets. An arm may only use a
  declared variant.

### Rules for config changes

- `harness`: needed to drive the server at all (offline mode, ports, console, helper plugin, raised per-IP limits).
- `parity`: aligns the server with the scenario (distances, seed, difficulty, heap).
- `rescue`: the default config fails the workload. State the default-config outcome in `docs/servers/chorus.md`; the
  report prints it as a footnote.
- `nonvanilla`: changes gameplay to save work. Always footnoted, usually as a separate variant.

Turn off telemetry, crash upload and auto-update where the server has a switch, as `harness` overrides. Do not change
anything else.

## 4. Templates and helpers

`servers/chorus/` holds config templates and helper plugin sources, each source file with an SPDX header. Helpers stay
minimal: a tick counter, a positions command, a raised per-IP packet limit if the server has one and no config key.
They never change simulation. Helpers are built locally against the server at `prepare()` time; no binaries are
committed.

## 5. Declare feature parity

`features/chorus.toml`:

```toml
schema = 1
server = "chorus"

[range."1.4"]
fidelity = "V1"
"terrain.vanilla_generator"            = { class = "absent", evidence = "https://github.com/example/chorus/blob/v1.4.2/README.md#worlds", note = "own generator" }
"terrain.any_generator"                = { class = "partial", evidence = "https://github.com/example/chorus/tree/v1.4.2/world/gen" }
"chunks.client_side_generation"        = { class = "absent", observed_by = "csg_effective" }
"chunks.subchunk_requests"             = { class = "vanilla", evidence = "https://github.com/example/chorus/blob/v1.4.2/net/subchunk.go" }
"player.server_authoritative_movement" = { class = "absent", observed_by = "corrections_per_bot_s" }
"player.physics"                       = { class = "absent", observed_by = "y_distribution_steady" }
"player.locator_bar"                   = { class = "absent", observed_by = "locator_pkts_per_bot_s" }
"world.autosave"                       = { class = "vanilla", evidence = "https://github.com/example/chorus/blob/v1.4.2/docs/config.md#autosave" }
```

- The range key is a version prefix; the longest matching prefix wins.
- Classes: `vanilla`, `partial`, `altered`, `absent`, `unknown`. Every class but `unknown` needs an `evidence` link or
  an `observed_by` metric. Evidence must be public: source at a pinned tag or commit, or official documentation.
- Fidelity: `V0` runs vanilla code paths, `V1` reimplements vanilla behaviour, `V2` changes gameplay.
- Cover at least every key that any scenario `exercises`. The keys are listed in `features/README.md`.

## 6. Document the server

`docs/servers/chorus.md` covers: pinned versions and where they come from, how the adapter sets the server up, every
override with category and reason, what happens with the default config (crashes, watchdogs, flood guards), the
tick-counter and positions mechanism, and the reasoning behind each parity class.

## 7. Test

- Add fixture lines to `tests/fixtures/logs/chorus.txt`: the ready line, the version banner, a player-in-world line, a
  tick reply, a positions reply and an error line. Write them by hand in the server's line shape; do not paste real
  server output beyond what is needed to show the shape.
- `tests/unit/test_adapter_chorus.py`: the prefix strips, levels map, positions parse, rendered configs carry the right
  overrides.
- Run the contract test, which every registered adapter must pass:

  ```shell
  uv run pytest tests/unit/test_adapters_contract.py -k chorus
  ```

  It checks that attributes are present, regexes compile and match the fixtures, rendered commands match their
  snapshots, every chaos action is rendered or marked unsupported, compatibility flags are in the allowlist, and the
  feature file exists and covers every exercised key.

- Run a smoke on every platform the adapter declares:

  ```shell
  uv run bbench fetch chorus@1.4.2
  uv run bbench smoke chorus@1.4.2
  ```

  The smoke uses 4 bots and short phases. It passes when every dialect command is accepted, the tick counter advances
  at idle, the bots decode every packet, the spread is valid, and corrections appear if the feature file declares
  server-authoritative movement.

## 8. Open the pull request

Use the adapter checklist in [CONTRIBUTING.md](../CONTRIBUTING.md#adding-or-updating-a-server) and attach the smoke's
`run.json`.

## Adapters outside this repository

A separate package can register adapters through the `bedrock_bench.adapters` entry point group:

```toml
[project.entry-points."bedrock_bench.adapters"]
chorus = "chorus_bench.adapter"          # a module, or "chorus_bench.adapter:ChorusAdapter" for one class
```

Out-of-tree adapters follow the same contract, but their results cannot be published from this repository until the
adapter, its feature file and its artifacts are merged here.
