# SPDX-License-Identifier: Apache-2.0
"""Dragonfly, a Bedrock server written in Go (https://github.com/df-mc/dragonfly), run through the dfbench helper.

Dragonfly is a library with an example server and no binary releases. ``servers/dragonfly`` holds dfbench:
that example server plus a console on standard input. Fetch builds it against the Dragonfly module pinned in the
folder's ``server.toml``, which must be the version its ``go.mod`` requires. Go verifies the module against go.sum;
fetch also checks the module zip against its pinned sha256, and records the build's defaults (``dfbench -defaults``)
next to the binary. See servers/dragonfly/README.md.

- Config: ``config.toml`` gets only the keys of the ``[config]`` table of ``servers/dragonfly/server.toml``.
  Every other key keeps the build's default, and each written value that differs from its default is an override.
- Console: Go's standard log format with a level; ``gametime`` reads the overworld's tick counter, ``positions``
  lists every player, and ``Player spawned: <name>`` is printed once a player is in the world.
- Dragonfly only generates flat worlds.
- A Go program has no tick thread to single out; only process CPU is reported.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import __version__, bots, config, fetch
from bedrock_bench.adapters.base import Dialect, ServerAdapter, host_os
from bedrock_bench.config import ConfigError
from bedrock_bench.model import (
    ArtifactRef,
    ConfigFile,
    Install,
    LaunchSpec,
    Layout,
    LogLine,
    Override,
    OverrideCategory,
    PlayerPos,
    RunContext,
    WorldInfo,
)
from bedrock_bench.provenance import sha256_file

if TYPE_CHECKING:
    import psutil

    from bedrock_bench.cache import Cache

SOURCE_DIR = "servers/dragonfly"  # dfbench, relative to the project root
SETTINGS_FILE = "servers/dragonfly/server.toml"  # its [config] table
CONFIG = "config.toml"  # what dfbench reads in the server directory
DEFAULTS = "defaults.toml"  # 'dfbench -defaults' of the build, kept in the install
BUILD_INFO = "build.json"  # 'dfbench -version' of the build and the module it links, kept in the install
FACTS = "dfbench.json"  # written by dfbench when the server starts
SERVER_NAME = "bedrock-bench"

#: Go runtime settings that would change how the server runs; a run never inherits them from the user's shell.
GO_RUNTIME_ENV = ("GOMAXPROCS", "GOGC", "GOMEMLIMIT", "GODEBUG")

BUILD_TIMEOUT_S = 1800.0
PROBE_TIMEOUT_S = 60.0

# --------------------------------------------------------------------------------------------------------------------
# Console dialect
# --------------------------------------------------------------------------------------------------------------------

#: ``2026/10/12 13:58:00.123456 INFO text``: Go's log package with microseconds, carrying slog's level. Chat
#: broadcasts (``a07 joined the game``) and Go panics are printed without it.
DRAGONFLY_PREFIX = re.compile(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d(?:\.\d+)? (?P<level>[A-Z]+)(?:[+-]\d+)? ")

DRAGONFLY_DIALECT = Dialect(
    ready=re.compile(r"^Server started\."),
    prefix=DRAGONFLY_PREFIX,
    level_map={"DEBUG": "debug", "INFO": "info", "WARN": "warn", "ERROR": "error"},
    benign=(),
    version=re.compile(r"^Dragonfly version (?P<version>\S+)"),
    player_in_world=re.compile(r"^Player spawned: (?P<player>.+)$"),
    tick_cmd="gametime",
    tick_re=re.compile(r"^Gametime is (?P<tick>\d+)$"),
)

POSITIONS_HEADER = re.compile(r"^Positions: (?P<n>\d+)$")
POSITION_LINE = re.compile(r"^Position: (?P<dim>\d) (?P<x>\S+) (?P<y>\S+) (?P<z>\S+) (?P<name>.+)$")


def parse_positions(lines: Sequence[LogLine]) -> list[PlayerPos] | None:
    """Positions from the reply to ``positions``: a ``Positions: N`` line, then one ``Position:`` line per player.

    None while the reply is missing or incomplete; other output may come in between.
    """
    for index, line in enumerate(lines):
        header = POSITIONS_HEADER.match(line.text)
        if header is None:
            continue
        expected = int(header["n"])
        found: list[PlayerPos] = []
        for later in lines[index + 1 :]:
            if len(found) == expected:
                break
            match = POSITION_LINE.match(later.text)
            if match is None:
                continue
            try:
                x, y, z = float(match["x"]), float(match["y"]), float(match["z"])
            except ValueError:
                return None
            found.append(PlayerPos(name=match["name"], x=x, y=y, z=z, dim=int(match["dim"])))
        return found if len(found) == expected else None
    return None


# --------------------------------------------------------------------------------------------------------------------
# config.toml
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Setting:
    """One value from the ``[config]`` table of ``servers/dragonfly/server.toml``."""

    key: str  # dotted path into config.toml, e.g. "Players.MaxCount"
    template: Any  # a TOML value; a string may hold {placeholders}
    category: OverrideCategory
    reason: str


def load_settings(path: Path) -> tuple[Setting, ...]:
    """Read the ``[config.file."config.toml"]`` table of the server file: ``"Section.Key" = {value, reason}``."""
    path, formats, entries = config.load_config_table(path)
    config.single_config_file(path, formats, CONFIG, "toml")
    settings: list[Setting] = []
    seen: set[str] = set()
    for e in entries:
        if not isinstance(e.entry, dict) or set(e.entry) != {"value", "reason"}:
            raise ConfigError('must be { value = ..., reason = "..." }', file=path, key=e.where)
        if not isinstance(e.entry["reason"], str) or not e.entry["reason"]:
            raise ConfigError("reason must be a non-empty string", file=path, key=e.where)
        if not re.fullmatch(r"[A-Za-z]\w*(?:\.[A-Za-z]\w*)+", e.key):
            raise ConfigError("must be a dotted path such as Players.MaxCount", file=path, key=e.where)
        if e.key in seen:
            raise ConfigError("key appears in two categories", file=path, key=e.where)
        seen.add(e.key)
        settings.append(Setting(e.key, e.entry["value"], e.category, e.entry["reason"]))
    return tuple(settings)


def settings_context(ctx: RunContext) -> dict[str, str]:
    """Placeholder values for the settings file."""
    server, world = ctx.workload.server, ctx.workload.world
    return {
        "server_name": SERVER_NAME,
        "port": str(ctx.machine.port),
        "view_distance": str(server.view_distance),
        "tick_distance": str(server.tick_distance),
        "max_players": str(server.max_players),
        "difficulty": server.difficulty,
        "gamemode": server.gamemode,
        "autosave_s": str(round(server.autosave_ticks / 20)),
        "save_player_data": "true" if server.save_player_data else "false",
        "level_name": world.level_name,
    }


def lookup(tree: Mapping[str, Any], key: str) -> tuple[bool, Any]:
    """``(True, value)`` for a dotted key present in a parsed TOML document, else ``(False, None)``."""
    node: Any = tree
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def render_settings(
    settings: Sequence[Setting], defaults: Mapping[str, Any], context: Mapping[str, str]
) -> tuple[dict[str, Any], list[Override]]:
    """The values to write, keyed by dotted path, and the overrides among them.

    Every key must exist in the build's defaults; values take the type of the default they replace.
    """
    values: dict[str, Any] = {}
    overrides: list[Override] = []
    for setting in settings:
        found, default = lookup(defaults, setting.key)
        if not found or isinstance(default, Mapping):
            raise ConfigError(f"{setting.key} is not a setting of this Dragonfly build (see 'dfbench -defaults')")
        template = setting.template
        if isinstance(template, str):
            try:
                template = template.format_map(context)
            except (KeyError, IndexError, ValueError) as exc:
                known = ", ".join(sorted(context))
                raise ConfigError(
                    f"{setting.key}: bad placeholder in {setting.template!r} ({exc}); known: {known}"
                ) from None
        value = _coerce(setting.key, template, default)
        values[setting.key] = value
        if value != default:
            overrides.append(
                Override(setting.category, CONFIG, setting.key, toml_text(default), toml_text(value), setting.reason)
            )
    return values, overrides


def _coerce(key: str, value: Any, default: Any) -> Any:
    try:
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            lowered = str(value).strip().lower()
            if lowered in ("true", "false"):
                return lowered == "true"
            raise ValueError(value)
        if isinstance(default, int):
            if isinstance(value, bool):
                raise ValueError(value)
            return int(value)
        if isinstance(default, str):
            if not isinstance(value, str):
                raise ValueError(value)
            return value
        if isinstance(default, list):
            if not isinstance(value, list):
                raise ValueError(value)
            return list(value)
    except ValueError:
        raise ConfigError(f"{key}: {value!r} is not a {type(default).__name__} like its default") from None
    raise ConfigError(f"{key}: cannot set a {type(default).__name__}")


def toml_text(value: Any) -> str:
    """A value as written in TOML, without quotes around a plain string (for run.json overrides)."""
    if isinstance(value, str):
        return value
    return _toml_value(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # a TOML basic string
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} as TOML")


def dump_toml(values: Mapping[str, Any]) -> str:
    """A TOML document from dotted keys: ``{"Players.MaxCount": 200}`` -> ``[Players]`` / ``MaxCount = 200``."""
    tables: dict[str, list[str]] = {}
    for key, value in values.items():
        table, _, name = key.rpartition(".")
        tables.setdefault(table, []).append(f"{name} = {_toml_value(value)}")
    blocks = [f"[{table}]\n" + "\n".join(lines) + "\n" for table, lines in tables.items()]
    return "\n".join(blocks)


# --------------------------------------------------------------------------------------------------------------------
# Building dfbench
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModulePin:
    """The Dragonfly module behind one server tag in ``servers/dragonfly/server.toml``."""

    module: str
    version: str  # module version, as go.mod requires it
    go_sum: str  # "h1:..." hash, as go.sum records it
    ref: ArtifactRef  # the module zip on the Go module proxy

    @classmethod
    def load(cls, tag_version: str) -> ModulePin:
        catalog = config.load_artifacts("dragonfly")
        try:
            spec = catalog.get(tag_version, "any")
        except KeyError as exc:
            raise ConfigError(str(exc.args[0]), file=catalog.path) from None
        missing = [key for key in ("module", "module_version", "go_sum") if not isinstance(spec.extra.get(key), str)]
        if missing or not spec.sha256:
            raise ConfigError(
                f"version {tag_version} needs module, module_version, go_sum and sha256", file=catalog.path
            )
        ref = ArtifactRef(name=spec.name, url=spec.url, sha256=spec.sha256, size=spec.size, source=spec.source)
        return cls(spec.extra["module"], spec.extra["module_version"], spec.extra["go_sum"], ref)


def required_version(go_mod: str, module: str) -> str | None:
    """The version of ``module`` that a go.mod file requires, or None."""
    escaped = re.escape(module)
    match = re.search(rf"^\s*(?:require\s+)?{escaped}\s+(\S+)", go_mod, re.MULTILINE)
    return match.group(1) if match else None


def check_source(src: Path, pin: ModulePin) -> None:
    """The dfbench module must build exactly the pinned module: same version in go.mod and go.sum, no replace."""
    try:
        go_mod = (src / "go.mod").read_text(encoding="utf-8")
        go_sum = (src / "go.sum").read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read the dfbench module: {exc}") from None
    required = required_version(go_mod, pin.module)
    if required != pin.version:
        raise ConfigError(
            f"{SOURCE_DIR}/go.mod requires {pin.module} {required}, but {SETTINGS_FILE} pins "
            f"{pin.version}; run 'go get {pin.module}@{pin.version}' there, or pin the version go.mod requires"
        )
    if re.search(r"^\s*replace\b", go_mod, re.MULTILINE):
        raise ConfigError(f"{SOURCE_DIR}/go.mod must not replace modules: a benchmark builds published code only")
    if f"{pin.module} {pin.version} {pin.go_sum}" not in go_sum:
        raise ConfigError(f"{SOURCE_DIR}/go.sum does not record {pin.go_sum} for {pin.module} {pin.version}")


def source_sha256(src: Path) -> str:
    """Hash of what the binary is built from: go.mod, go.sum and the non-test Go files, line endings normalised."""
    digest = hashlib.sha256()
    files = sorted(p for p in src.iterdir() if p.name in ("go.mod", "go.sum") or p.name.endswith(".go"))
    for path in files:
        if path.name.endswith("_test.go"):
            continue
        content = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(f"{path.name}\0{hashlib.sha256(content).hexdigest()}\n".encode())
    return digest.hexdigest()


def exe_name(target: bots.Target) -> str:
    return "dfbench" + target.exe_suffix


def _go_env(target: bots.Target) -> dict[str, str]:
    env = bots.build_env(target)
    if target.goarch == "amd64":
        env["GOAMD64"] = "v1"  # the baseline instruction set, whatever the user's environment says
    return env


def run_command(
    argv: Sequence[str],
    *,
    what: str,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> str:
    """Run a command and return its stdout; any failure is a :class:`fetch.FetchError` naming ``what``."""
    try:
        done = subprocess.run(
            list(argv),
            cwd=cwd,
            env=None if env is None else dict(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise fetch.FetchError(f"{what}: cannot run {argv[0]}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise fetch.FetchError(f"{what}: no result after {timeout_s:.0f} s") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()[-20:]
        raise fetch.FetchError(f"{what} failed with exit code {done.returncode}" + "".join(f"\n  {d}" for d in detail))
    return done.stdout


def _json_object(text: str, what: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise fetch.FetchError(f"{what} printed invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise fetch.FetchError(f"{what} printed no JSON object")
    return value


def describe_binary(
    exe: Path, out_dir: Path, *, target: bots.Target, expect_version: str, expect_protocol: int
) -> None:
    """Record a runnable dfbench in ``out_dir``: its ``-version`` JSON plus the target it was built for as build.json,
    and its ``-defaults`` as defaults.toml.

    The build must link the expected Dragonfly version and speak the expected protocol.
    """
    info = _json_object(run_command([str(exe), "-version"], what="dfbench -version"), "dfbench -version")
    if info.get("dragonfly") != expect_version:
        raise fetch.FetchError(
            f"{exe.name} links Dragonfly {info.get('dragonfly')} ({info.get('dragonfly_module')}), not {expect_version}"
        )
    if info.get("protocol") != expect_protocol:
        raise fetch.FetchError(f"{exe.name} speaks protocol {info.get('protocol')}, the bots {expect_protocol}")
    defaults = run_command([str(exe), "-defaults"], what="dfbench -defaults")
    try:
        tomllib.loads(defaults)
    except tomllib.TOMLDecodeError as exc:
        raise fetch.FetchError(f"dfbench -defaults printed invalid TOML: {exc}") from exc
    (out_dir / DEFAULTS).write_text(defaults, encoding="utf-8", newline="\n")
    info["target"] = str(target)
    (out_dir / BUILD_INFO).write_text(json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def build_dfbench(
    go: str, src: Path, pin: ModulePin, target: bots.Target, out_dir: Path, *, expect_version: str, expect_protocol: int
) -> None:
    """Verify the pinned module, build dfbench for ``target`` into ``out_dir`` and describe the build there."""
    output = run_command(
        [go, "mod", "download", "-json", f"{pin.module}@{pin.version}"],
        what="go mod download",
        cwd=src,
        env=_go_env(bots.Target.host()),
        timeout_s=BUILD_TIMEOUT_S,
    )
    download = _json_object(output, "go mod download")
    if download.get("Sum") != pin.go_sum:
        raise fetch.FetchError(f"{pin.module} {pin.version} has checksum {download.get('Sum')}, pinned {pin.go_sum}")
    zip_path = Path(download.get("Zip", ""))
    if not zip_path.is_file():
        raise fetch.FetchError(f"go mod download did not report the zip of {pin.module} {pin.version}")
    actual, size = sha256_file(zip_path), zip_path.stat().st_size
    if actual != pin.ref.sha256 or (pin.ref.size is not None and size != pin.ref.size):
        raise fetch.FetchError(
            f"the {pin.module} {pin.version} zip in the Go module cache (sha256 {actual}, {size} bytes) is not the "
            f"pinned {pin.ref.url} (sha256 {pin.ref.sha256}); download modules through https://proxy.golang.org"
        )

    exe = out_dir / exe_name(target)
    run_command(
        bots.build_command(go, __version__, source_sha256(src)[:12], exe),
        what=f"go build of dfbench for {target}",
        cwd=src,
        env=_go_env(target),
        timeout_s=BUILD_TIMEOUT_S,
    )
    describe_binary(exe, out_dir, target=target, expect_version=expect_version, expect_protocol=expect_protocol)


# --------------------------------------------------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------------------------------------------------


class DragonflyAdapter(ServerAdapter):
    name: ClassVar[str] = "dragonfly"
    platforms: ClassVar[frozenset[str]] = frozenset({"windows", "linux"})
    dialect: ClassVar[Dialect] = DRAGONFLY_DIALECT
    tick_thread_rule: ClassVar[str] = "none"

    @property
    def target(self) -> bots.Target:
        """The Go target the server runs on: this machine."""
        try:
            host = bots.Target.host()
        except bots.BotError as exc:
            raise ConfigError(str(exc)) from None
        if host.goos not in ("windows", "linux"):
            raise ConfigError(f"Dragonfly runs on Windows and Linux here, not on {host_os()}")
        return host

    # ---- fetch ----------------------------------------------------------------------------------------------------

    def fetch(self, cache: Cache) -> Install:
        """Build dfbench against the pinned module, or adopt a prebuilt dfbench given as the arm's artifact."""
        target = self.target
        if self.arm.artifact is not None:
            return self._adopt(cache, target)
        pin = ModulePin.load(self.arm.version)
        src = config.project_root() / SOURCE_DIR
        check_source(src, pin)
        go = shutil.which("go")
        if go is None:
            raise fetch.FetchError("building dfbench needs the Go toolchain (1.26 or later) on PATH")
        go_version = run_command([go, "env", "GOVERSION"], what="go env", cwd=src, env=_go_env(target)).strip()
        key = ("dragonfly", self.arm.version, target.slug, source_sha256(src), go_version, __version__)
        root = cache.install_dir(*key)
        tree = fetch.ensure_install(
            cache,
            root,
            lambda tmp: build_dfbench(
                go, src, pin, target, tmp, expect_version=self.arm.version, expect_protocol=self.protocol
            ),
        )
        return self._install(root, tree, pin.ref, target)

    def _adopt(self, cache: Cache, target: bots.Target) -> Install:
        assert self.arm.artifact is not None
        original, ref = fetch.adopt_local(self.arm.artifact, cache)
        root = cache.install_dir("dragonfly", self.arm.version, target.slug, "artifact", ref.sha256)

        def install(tmp: Path) -> None:
            exe = tmp / exe_name(target)
            shutil.copyfile(original, exe)
            if os.name == "posix":
                os.chmod(exe, stat.S_IMODE(exe.stat().st_mode) | 0o755)
            describe_binary(exe, tmp, target=target, expect_version=self.arm.version, expect_protocol=self.protocol)

        tree = fetch.ensure_install(cache, root, install)
        return self._install(root, tree, None, target)

    def _install(self, root: Path, tree: str, module: ArtifactRef | None, target: bots.Target) -> Install:
        exe = root / exe_name(target)
        info = json.loads((root / BUILD_INFO).read_text(encoding="utf-8"))
        built = ArtifactRef(
            name=exe.name,
            url=None,
            sha256=sha256_file(exe),
            size=exe.stat().st_size,
            source="build" if module is not None else "local",
        )
        return Install(
            root=root,
            tree_sha256=tree,
            artifacts=(module, built) if module is not None else (built,),
            extra={
                "exe": exe.name,
                "exe_sha256": built.sha256,
                "target": str(target),
                "dragonfly_module": info.get("dragonfly_module"),
                "go": info.get("go"),
                "dfbench": info.get("version"),
            },
        )

    # ---- prepare and launch ---------------------------------------------------------------------------------------

    def prepare(self, install: Install, ctx: RunContext) -> Layout:
        server_dir = ctx.work_dir
        fetch.copy_install(install.root, server_dir)
        exe_rel = install.extra["exe"]
        exe_sha256 = sha256_file(server_dir / exe_rel)
        if exe_sha256 != install.extra["exe_sha256"]:
            raise fetch.FetchError(f"the fresh copy of {exe_rel} does not match the verified install")

        defaults = config.load_toml(server_dir / DEFAULTS)
        settings = load_settings(config.project_root() / SETTINGS_FILE)
        values, overrides = render_settings(settings, defaults, settings_context(ctx))
        folder = lookup(defaults, "World.Folder")[1]
        world = self._prepare_world(server_dir / str(values.get("World.Folder", folder)), ctx)

        # Dragonfly's tables in alphabetical order, then [Bench]; keys keep the order of the settings file.
        ordered = dict(sorted(values.items(), key=lambda kv: (kv[0].startswith("Bench."), kv[0].rpartition(".")[0])))
        path = server_dir / CONFIG
        path.write_text(dump_toml(ordered), encoding="utf-8", newline="\n")
        return Layout(
            server_dir=server_dir,
            overrides=tuple(overrides),
            config_files=(ConfigFile(CONFIG, sha256_file(path)),),
            world=world,
            extra={
                "exe": exe_rel,
                "exe_sha256": exe_sha256,
                "settings": values,
                "dragonfly_module": install.extra.get("dragonfly_module"),
                "go": install.extra.get("go"),
            },
        )

    def _prepare_world(self, level_dir: Path, ctx: RunContext) -> WorldInfo:
        if level_dir.exists():
            raise RuntimeError(f"the fresh server copy already has a world at {level_dir}")
        return WorldInfo(fresh=True, generator="flat", path=level_dir)

    def launch(self, layout: Layout) -> LaunchSpec:
        return LaunchSpec(
            argv=(str(self.executable(layout)), "-config", CONFIG),
            cwd=layout.server_dir,
            env_remove=GO_RUNTIME_ENV,
        )

    def executable(self, layout: Layout) -> Path:
        """The dfbench binary in the run's copy; its sha256 is checked before and after the run."""
        return layout.server_dir / layout.extra["exe"]

    def readback(self, layout: Layout) -> list[str]:
        """Written config.toml values that changed while the server ran; empty when all read back."""
        path = layout.server_dir / CONFIG
        try:
            found = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return [f"{CONFIG} cannot be read after the run: {exc}"]
        problems = []
        for key, value in layout.extra.get("settings", {}).items():
            present, now = lookup(found, key)
            if not present or now != value:
                problems.append(f"{CONFIG}: {key} was written as {value!r} and read back as {now!r}")
        return problems

    # ---- commands -------------------------------------------------------------------------------------------------

    def cmd_positions(self) -> str | None:
        return "positions"

    def parse_positions(self, lines: list[LogLine]) -> list[PlayerPos] | None:
        return parse_positions(lines)

    # ---- facts ----------------------------------------------------------------------------------------------------

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        """Build and runtime facts from what dfbench wrote at startup, plus the written settings."""
        try:
            runtime = json.loads((layout.server_dir / FACTS).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            runtime = {}
        settings = layout.extra.get("settings", {})
        return {
            "allocator": "go",
            "csg_setting": False,
            "dragonfly_module": runtime.get("dragonfly_module", layout.extra.get("dragonfly_module")),
            "go": runtime.get("go", layout.extra.get("go")),
            "gomaxprocs": runtime.get("gomaxprocs"),
            "num_cpu": runtime.get("num_cpu"),
            "difficulty": runtime.get("difficulty"),
            "default_game_mode": runtime.get("default_game_mode"),
            "tick_range": runtime.get("tick_range", settings.get("Bench.TickRange")),
            "save_interval_s": runtime.get("save_interval_s"),
            "idle_ticking": runtime.get("idle_ticking", settings.get("Bench.IdleTicking")),
        }


__all__ = [
    "DRAGONFLY_DIALECT",
    "DragonflyAdapter",
    "ModulePin",
    "Setting",
    "build_dfbench",
    "check_source",
    "describe_binary",
    "dump_toml",
    "load_settings",
    "lookup",
    "parse_positions",
    "render_settings",
    "required_version",
    "settings_context",
    "source_sha256",
]
