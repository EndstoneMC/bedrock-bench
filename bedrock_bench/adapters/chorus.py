# SPDX-License-Identifier: Apache-2.0
"""Chorus, a Bedrock server written in Rust (https://github.com/bedrock-crustaceans/chorus), run through chorusbench.

Chorus has no releases or tags. ``configs/chorus`` holds chorusbench: the Chorus server plus the console
commands the harness needs. Fetch builds it with ``cargo build --release --locked`` on the Rust toolchain pinned in the
folder's ``server.toml``, from the ``Cargo.lock`` committed next to it, which pins the Chorus commit and every
dependency. ``chorusbench -version`` must report the pinned commit and the bots' protocol; ``chorusbench -defaults``
is kept next to the binary. See configs/chorus/README.md.

- Config: ``chorus.toml`` gets only the keys of the ``[config]`` table of ``configs/chorus/server.toml``.
  Chorus fills in every other key, and each written value that differs from the build's default is an override.
- Console: ``HH:MM:SS LEVEL text`` log lines; command replies have no prefix. chorusbench adds ``gametime``,
  ``positions``, ``tpp`` (teleport another player) and ``kick``, and logs their failures at error level.
- The Bevy schedule runs on the main thread, the first one created; the task pools are diagnostic groups.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bedrock_bench import __version__, config, fetch
from bedrock_bench.adapters.base import Dialect, ServerAdapter, format_coord, host_os, quote_player
from bedrock_bench.adapters.dragonfly import dump_toml, lookup, run_command, toml_text
from bedrock_bench.adapters.standalone import parse_helper_positions
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

SOURCE_DIR = "configs/chorus"  # chorusbench, relative to the project root
SETTINGS_FILE = "configs/chorus/server.toml"  # its [config] table
REPOSITORY = "https://github.com/bedrock-crustaceans/chorus"
CONFIG = "chorus.toml"  # what Chorus reads in the server directory
DEFAULTS = "defaults.toml"  # 'chorusbench -defaults' of the build, kept in the install
BUILD_INFO = "build.json"  # 'chorusbench -version' of the build, kept in the install
FACTS = "chorusbench.json"  # written by chorusbench when the server starts
EXE = "chorusbench.exe" if os.name == "nt" else "chorusbench"

#: Build settings from the user's environment that would change the binary or where it lands.
CARGO_ENV_REMOVE = (
    "RUSTFLAGS",
    "CARGO_ENCODED_RUSTFLAGS",
    "CARGO_BUILD_RUSTFLAGS",
    "CARGO_BUILD_TARGET",
    "CARGO_BUILD_TARGET_DIR",
)
CARGO_ENV_PREFIXES = ("CARGO_PROFILE_",)

BUILD_TIMEOUT_S = 3600.0
PROBE_TIMEOUT_S = 60.0

# --------------------------------------------------------------------------------------------------------------------
# Console dialect
# --------------------------------------------------------------------------------------------------------------------

#: ``17:27:00 INFO text``: tracing's level after a local time. Span names such as ``network`` stay in the text.
CHORUS_PREFIX = re.compile(r"^\d{2}:\d{2}:\d{2} (?P<level>TRACE|DEBUG|INFO|WARN|ERROR) ")

CHORUS_DIALECT = Dialect(
    ready=re.compile(r"^Started on \S+:\d+\.$"),
    prefix=CHORUS_PREFIX,
    level_map={"TRACE": "debug", "DEBUG": "debug", "INFO": "info", "WARN": "warn", "ERROR": "error"},
    benign=(),
    version=re.compile(r"^chorusbench (?P<version>[0-9a-f]{7}) \("),
    player_in_world=re.compile(r"^(?P<player>\S+) joined the game$"),
    tick_cmd="gametime",
    tick_re=re.compile(r"^Gametime is (?P<tick>\d+)$"),
)

# --------------------------------------------------------------------------------------------------------------------
# chorus.toml
# --------------------------------------------------------------------------------------------------------------------

KINDS = ("str", "int", "bool")


@dataclass(frozen=True)
class Setting:
    """One value from the ``[config]`` table of ``configs/chorus/server.toml``."""

    key: str  # dotted path into chorus.toml, e.g. "server.max_players"
    template: Any  # a TOML value; a string may hold {placeholders}
    category: OverrideCategory
    reason: str
    kind: str | None = None  # str, int or bool; None takes the type of the default


def load_settings(path: Path) -> tuple[Setting, ...]:
    """Read the ``[config.file."chorus.toml"]`` table: ``"section.key" = {value, reason, type?}`` entries."""
    path, formats, entries = config.load_config_table(path)
    config.single_config_file(path, formats, CONFIG, "toml")
    settings: list[Setting] = []
    seen: set[str] = set()
    for e in entries:
        entry, where = e.entry, e.where
        if not isinstance(entry, dict) or not {"value", "reason"} <= set(entry) <= {"value", "reason", "type"}:
            raise ConfigError('must be { value = ..., reason = "..." } with an optional type', file=path, key=where)
        if not isinstance(entry["reason"], str) or not entry["reason"]:
            raise ConfigError("reason must be a non-empty string", file=path, key=where)
        kind = entry.get("type")
        if kind is not None and kind not in KINDS:
            raise ConfigError(f"type must be one of {', '.join(KINDS)}", file=path, key=where)
        if not re.fullmatch(r"[a-z]\w*(?:\.[a-z]\w*)+", e.key):
            raise ConfigError("must be a dotted path such as server.max_players", file=path, key=where)
        if e.key in seen:
            raise ConfigError("key appears in two categories", file=path, key=where)
        seen.add(e.key)
        settings.append(Setting(e.key, entry["value"], e.category, entry["reason"], kind))
    return tuple(settings)


def settings_context(ctx: RunContext) -> dict[str, str]:
    """Placeholder values for the settings, from the scenario, the machine profile and the placement."""
    server, world = ctx.scenario.server, ctx.scenario.world
    return {
        "port": str(ctx.machine.port),
        "max_players": str(server.max_players),
        "view_distance": str(server.view_distance),
        "autosave_s": str(round(server.autosave_ticks / 20)),
        "level_name": world.level_name,
        "seed": str(world.seed),
        "server_cpu_count": str(len(ctx.placement.server_cpus)),
    }


def render_settings(
    settings: Sequence[Setting], defaults: Mapping[str, Any], context: Mapping[str, str]
) -> tuple[dict[str, Any], list[Override]]:
    """The values to write, keyed by dotted path, and the overrides among them.

    Every key must exist in the build's defaults.
    """
    values: dict[str, Any] = {}
    overrides: list[Override] = []
    for setting in settings:
        found, default = lookup(defaults, setting.key)
        if not found or isinstance(default, Mapping):
            raise ConfigError(f"{setting.key} is not a setting of this Chorus build (see 'chorusbench -defaults')")
        template = setting.template
        if isinstance(template, str):
            try:
                template = template.format_map(context)
            except (KeyError, IndexError, ValueError) as exc:
                known = ", ".join(sorted(context))
                raise ConfigError(
                    f"{setting.key}: bad placeholder in {setting.template!r} ({exc}); known: {known}"
                ) from None
        value = _coerce(setting.key, template, setting.kind or type(default).__name__)
        values[setting.key] = value
        if value != default:
            overrides.append(
                Override(setting.category, CONFIG, setting.key, toml_text(default), toml_text(value), setting.reason)
            )
    return values, overrides


def _coerce(key: str, value: Any, kind: str) -> Any:
    try:
        if kind == "bool":
            if isinstance(value, bool):
                return value
            lowered = str(value).strip().lower()
            if lowered in ("true", "false"):
                return lowered == "true"
            raise ValueError(value)
        if kind == "int":
            if isinstance(value, bool):
                raise ValueError(value)
            return int(value)
        if kind == "str":
            if not isinstance(value, str):
                raise ValueError(value)
            return value
    except ValueError:
        raise ConfigError(f"{key}: {value!r} is not a {kind}") from None
    raise ConfigError(f"{key}: cannot set a {kind}")


# --------------------------------------------------------------------------------------------------------------------
# Building chorusbench
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SourcePin:
    """The Chorus commit behind one server tag, and what the build of it must use."""

    commit: str
    protocol: int
    minecraft: str
    rust: str  # toolchain version, e.g. "1.95.0"
    cargo_lock: str  # sha256 of Cargo.lock with LF line endings

    @classmethod
    def load(cls, version: str) -> SourcePin:
        catalog = config.load_artifacts("chorus")
        entry = catalog.versions.get(version)
        if entry is None:
            pinned = ", ".join(sorted(catalog.versions))
            raise ConfigError(f"chorus {version} is not pinned (pinned: {pinned})", file=catalog.path)
        meta = entry.meta
        kinds = {"commit": str, "protocol": int, "minecraft": str, "rust": str, "cargo_lock": str}
        wrong = [key for key, kind in kinds.items() if not isinstance(meta.get(key), kind)]
        if wrong:
            raise ConfigError(f"version {version} needs {', '.join(wrong)}", file=catalog.path)
        if not re.fullmatch(r"[0-9a-f]{40}", meta["commit"]) or not meta["commit"].startswith(version):
            raise ConfigError(f"version {version}: commit must be the full hash that starts with it", file=catalog.path)
        return cls(meta["commit"], meta["protocol"], meta["minecraft"], meta["rust"], meta["cargo_lock"])


def lock_sha256(lock: bytes) -> str:
    """sha256 of a Cargo.lock with LF line endings, so a checkout with CRLF hashes the same."""
    return hashlib.sha256(lock.replace(b"\r\n", b"\n")).hexdigest()


def locked_commit(lock: str, package: str) -> str | None:
    """The commit a Cargo.lock pins for a git ``package`` (``source = "git+...#<commit>"``), or None."""
    for block in lock.split("[[package]]"):
        if re.search(rf'^name = "{re.escape(package)}"$', block, re.MULTILINE):
            source = re.search(r'^source = "git\+[^"#]+#([0-9a-f]{40})"$', block, re.MULTILINE)
            if source is not None:
                return source.group(1)
    return None


def check_source(src: Path, pin: SourcePin) -> None:
    """``configs/chorus`` must build exactly the pinned commit, from the pinned Cargo.lock, without patches."""
    try:
        manifest = (src / "Cargo.toml").read_text(encoding="utf-8")
        lock = (src / "Cargo.lock").read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read chorusbench's manifest: {exc}") from None
    revs = set(re.findall(rf'git\s*=\s*"{re.escape(REPOSITORY)}"\s*,\s*rev\s*=\s*"([^"]*)"', manifest))
    if revs != {pin.commit}:
        found = ", ".join(sorted(revs)) or "no rev"
        raise ConfigError(f"{SOURCE_DIR}/Cargo.toml names Chorus {found}, but {SETTINGS_FILE} pins {pin.commit}")
    if re.search(r"^\s*\[patch", manifest, re.MULTILINE):
        raise ConfigError(f"{SOURCE_DIR}/Cargo.toml must not patch dependencies: benchmarks build published code only")
    if lock_sha256(lock) != pin.cargo_lock:
        raise ConfigError(
            f"{SOURCE_DIR}/Cargo.lock has sha256 {lock_sha256(lock)}, but {SETTINGS_FILE} pins {pin.cargo_lock}; "
            "review the lock change, then pin its hash"
        )
    locked = locked_commit(lock.decode("utf-8", errors="replace"), "chorus")
    if locked != pin.commit:
        raise ConfigError(f"{SOURCE_DIR}/Cargo.lock locks Chorus {locked}, not {pin.commit}")


def source_sha256(src: Path) -> str:
    """Hash of what the binary is built from: Cargo.toml, Cargo.lock and the Rust files, line endings normalised."""
    digest = hashlib.sha256()
    files = sorted(p for p in src.iterdir() if p.name in ("Cargo.toml", "Cargo.lock") or p.suffix == ".rs")
    for path in files:
        content = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(f"{path.name}\0{hashlib.sha256(content).hexdigest()}\n".encode())
    return digest.hexdigest()


def cargo_env(rust: str, target_dir: Path | None, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of a cargo or rustc call: the pinned toolchain, and no build settings from the user's shell."""
    env = {
        key: value
        for key, value in (os.environ if base is None else base).items()
        if key not in CARGO_ENV_REMOVE and not key.startswith(CARGO_ENV_PREFIXES)
    }
    env["RUSTUP_TOOLCHAIN"] = rust
    if target_dir is not None:
        env["CARGO_TARGET_DIR"] = str(target_dir)
    else:
        env.pop("CARGO_TARGET_DIR", None)
    return env


def rustc_version(rustc: str, rust: str) -> str:
    """``rustc --version`` of the pinned toolchain; refuses any other version."""
    line = run_command([rustc, "--version"], what="rustc --version", env=cargo_env(rust, None)).strip()
    if not line.startswith(f"rustc {rust} "):
        raise fetch.FetchError(
            f"chorusbench builds with Rust {rust} ('rustup toolchain install {rust}'), but rustc reports {line!r}"
        )
    return line


def _json_object(text: str, what: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise fetch.FetchError(f"{what} printed invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise fetch.FetchError(f"{what} printed no JSON object")
    return value


def describe_binary(exe: Path, out_dir: Path, *, expect_version: str, expect_protocol: int) -> dict[str, Any]:
    """Record a runnable chorusbench in ``out_dir``: its ``-version`` JSON as build.json and its ``-defaults`` as
    defaults.toml.

    The build must link the Chorus commit of the server tag and speak the bots' protocol.
    """
    info = _json_object(run_command([str(exe), "-version"], what="chorusbench -version"), "chorusbench -version")
    if not str(info.get("chorus_commit", "")).startswith(expect_version):
        raise fetch.FetchError(f"{exe.name} links Chorus {info.get('chorus_commit')}, not {expect_version}")
    if info.get("protocol") != expect_protocol:
        raise fetch.FetchError(f"{exe.name} speaks protocol {info.get('protocol')}, the bots {expect_protocol}")
    defaults = run_command([str(exe), "-defaults"], what="chorusbench -defaults")
    try:
        tomllib.loads(defaults)
    except tomllib.TOMLDecodeError as exc:
        raise fetch.FetchError(f"chorusbench -defaults printed invalid TOML: {exc}") from exc
    (out_dir / DEFAULTS).write_text(defaults, encoding="utf-8", newline="\n")
    (out_dir / BUILD_INFO).write_text(json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return info


def install_binary(built: Path, out_dir: Path) -> Path:
    """Copy a chorusbench binary into ``out_dir`` with its exec bit."""
    exe = out_dir / EXE
    shutil.copyfile(built, exe)
    if os.name == "posix":
        os.chmod(exe, stat.S_IMODE(exe.stat().st_mode) | 0o755)
    return exe


def build_chorusbench(cargo: str, src: Path, pin: SourcePin, out_dir: Path, *, version: str, protocol: int) -> None:
    """Build chorusbench in a scratch target directory next to ``out_dir``, install it there and describe the build."""
    target_dir = out_dir.with_name(out_dir.name + "-target")
    try:
        run_command(
            [cargo, "build", "--release", "--locked", "--manifest-path", str(src / "Cargo.toml")],
            what="cargo build of chorusbench",
            cwd=src,
            env=cargo_env(pin.rust, target_dir),
            timeout_s=BUILD_TIMEOUT_S,
        )
        exe = install_binary(target_dir / "release" / EXE, out_dir)
    finally:
        shutil.rmtree(target_dir, ignore_errors=True)
    info = describe_binary(exe, out_dir, expect_version=version, expect_protocol=protocol)
    if info.get("chorus_commit") != pin.commit or info.get("minecraft") != pin.minecraft:
        raise fetch.FetchError(
            f"{EXE} reports Chorus {info.get('chorus_commit')} for Minecraft {info.get('minecraft')}, "
            f"pinned {pin.commit} for {pin.minecraft}"
        )


# --------------------------------------------------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------------------------------------------------


class ChorusAdapter(ServerAdapter):
    name: ClassVar[str] = "chorus"
    platforms: ClassVar[frozenset[str]] = frozenset({"windows", "linux"})
    protocol: ClassVar[int] = 2193
    dialect: ClassVar[Dialect] = CHORUS_DIALECT
    tick_thread_rule: ClassVar[str] = "first_created"
    thread_groups: ClassVar[Mapping[str, str]] = {
        "compute": r"^Compute Task",
        "async compute": r"^Async Compute",
        "io": r"^IO Task",
    }
    crash_paths: ClassVar[tuple[str, ...]] = ("logs",)

    # ---- fetch ----------------------------------------------------------------------------------------------------

    def fetch(self, cache: Cache) -> Install:
        """Build chorusbench from the pinned sources, or adopt a prebuilt one given as the arm's artifact."""
        if self.arm.artifact is not None:
            return self._adopt(cache)
        pin = SourcePin.load(self.arm.version)
        src = config.project_root() / SOURCE_DIR
        check_source(src, pin)
        cargo, rustc = shutil.which("cargo"), shutil.which("rustc")
        if cargo is None or rustc is None:
            raise fetch.FetchError(f"building chorusbench needs Rust {pin.rust} (cargo and rustc) on PATH")
        compiler = rustc_version(rustc, pin.rust)
        root = cache.install_dir("chorus", self.arm.version, host_os(), source_sha256(src), compiler, __version__)
        tree = fetch.ensure_install(
            cache,
            root,
            lambda tmp: build_chorusbench(cargo, src, pin, tmp, version=self.arm.version, protocol=self.protocol),
        )
        size = (src / "Cargo.lock").stat().st_size
        lock = ArtifactRef(name="Cargo.lock", url=None, sha256=pin.cargo_lock, size=size, source="build")
        return self._install(root, tree, lock)

    def _adopt(self, cache: Cache) -> Install:
        assert self.arm.artifact is not None
        original, ref = fetch.adopt_local(self.arm.artifact, cache)
        root = cache.install_dir("chorus", self.arm.version, host_os(), "artifact", ref.sha256)

        def install(tmp: Path) -> None:
            exe = install_binary(original, tmp)
            describe_binary(exe, tmp, expect_version=self.arm.version, expect_protocol=self.protocol)

        tree = fetch.ensure_install(cache, root, install)
        return self._install(root, tree, None)

    def _install(self, root: Path, tree: str, lock: ArtifactRef | None) -> Install:
        exe = root / EXE
        info = json.loads((root / BUILD_INFO).read_text(encoding="utf-8"))
        built = ArtifactRef(
            name=EXE,
            url=None,
            sha256=sha256_file(exe),
            size=exe.stat().st_size,
            source="build" if lock is not None else "local",
        )
        return Install(
            root=root,
            tree_sha256=tree,
            artifacts=(lock, built) if lock is not None else (built,),
            extra={
                "exe": EXE,
                "exe_sha256": built.sha256,
                "chorus_commit": info.get("chorus_commit"),
                "chorusbench": info.get("version"),
                "rustc": info.get("rustc"),
                "target": info.get("target"),
            },
        )

    # ---- prepare and launch ---------------------------------------------------------------------------------------

    def prepare(self, install: Install, ctx: RunContext) -> Layout:
        if ctx.scenario.world.source == "template":
            raise ConfigError(
                f"scenario {ctx.scenario.name} needs a world template, but Chorus saves only the chunks that changed"
            )
        server_dir = ctx.work_dir
        fetch.copy_install(install.root, server_dir)
        exe_rel = install.extra["exe"]
        exe_sha256 = sha256_file(server_dir / exe_rel)
        if exe_sha256 != install.extra["exe_sha256"]:
            raise fetch.FetchError(f"the fresh copy of {exe_rel} does not match the verified install")

        defaults = config.load_toml(server_dir / DEFAULTS)
        settings = load_settings(config.project_root() / SETTINGS_FILE)
        values, overrides = render_settings(settings, defaults, settings_context(ctx))
        level = server_dir / "worlds" / str(values["level.name"])
        if level.exists():
            raise RuntimeError(f"the fresh server copy already has a world at {level}")
        path = server_dir / CONFIG
        path.write_text(dump_toml(values), encoding="utf-8", newline="\n")
        commands = Override(
            OverrideCategory.HARNESS,
            "chorusbench",
            "console commands",
            None,
            "gametime, positions, tpp, kick",
            "Chorus's console has no tick counter, no position list, and no teleport or kick of another player",
        )
        return Layout(
            server_dir=server_dir,
            overrides=(*overrides, commands),
            config_files=(ConfigFile(CONFIG, sha256_file(path)),),
            world=WorldInfo(fresh=True, generator="custom", path=level),
            extra={
                "exe": exe_rel,
                "exe_sha256": exe_sha256,
                "settings": values,
                **{key: install.extra.get(key) for key in ("chorus_commit", "chorusbench", "rustc", "target")},
            },
        )

    def launch(self, layout: Layout) -> LaunchSpec:
        return LaunchSpec(argv=(str(self.executable(layout)),), cwd=layout.server_dir)

    def executable(self, layout: Layout) -> Path:
        """The chorusbench binary in the run's copy; its sha256 is checked before and after the run."""
        return layout.server_dir / layout.extra["exe"]

    def readback(self, layout: Layout) -> list[str]:
        """Written chorus.toml values that changed while the server ran; empty when all read back."""
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

    def cmd_effects(self, player: str) -> list[str]:
        """Nothing: Chorus has no effects, and no damage, hunger, fire or drowning to guard against."""
        return []

    def cmd_tp(self, player: str, x: float, y: float, z: float) -> str:
        return f"tpp {quote_player(player)} {format_coord(x)} {format_coord(y)} {format_coord(z)}"

    def cmd_positions(self) -> str | None:
        return "positions"

    def parse_positions(self, lines: list[LogLine]) -> list[PlayerPos] | None:
        return parse_helper_positions(lines)

    # ---- facts ----------------------------------------------------------------------------------------------------

    def facts(self, layout: Layout, proc: psutil.Process) -> dict[str, Any]:
        """Build and runtime facts from what chorusbench wrote at startup, plus what Chorus fixes in code."""
        try:
            runtime = json.loads((layout.server_dir / FACTS).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            runtime = {}
        build = runtime.get("build") or {}
        return {
            "allocator": "default",
            "csg_setting": False,
            "chorus_commit": build.get("chorus_commit", layout.extra.get("chorus_commit")),
            "rustc": build.get("rustc", layout.extra.get("rustc")),
            "target": build.get("target", layout.extra.get("target")),
            "threads": runtime.get("threads"),
            "available_parallelism": runtime.get("available_parallelism"),
            "compute_threads": runtime.get("compute_threads"),
            "async_compute_threads": runtime.get("async_compute_threads"),
            "io_threads": runtime.get("io_threads"),
            "chunk_saving": runtime.get("chunk_saving"),
            "autosave": runtime.get("autosave"),
            "compression": runtime.get("compression"),
            "zlib": runtime.get("zlib"),
            "raknet_encryption": runtime.get("raknet_encryption"),
            "difficulty": "peaceful (fixed by Chorus)",
            "default_game_mode": "survival (fixed by Chorus)",
            "tick_range": 0,
        }


__all__ = [
    "CHORUS_DIALECT",
    "ChorusAdapter",
    "Setting",
    "SourcePin",
    "build_chorusbench",
    "cargo_env",
    "check_source",
    "describe_binary",
    "load_settings",
    "lock_sha256",
    "locked_commit",
    "render_settings",
    "rustc_version",
    "settings_context",
    "source_sha256",
]
