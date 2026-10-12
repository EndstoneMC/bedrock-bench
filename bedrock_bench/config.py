# SPDX-License-Identifier: Apache-2.0
"""Load and validate the TOML inputs: server files and machine profiles.

Every loader rejects unknown keys, so a typo fails loudly instead of falling back to a default. Failures raise
:class:`ConfigError` naming the file and the dotted key; the CLI turns them into exit code 2.

Server files are referenced by path or by bare name. A bare name resolves inside the project checkout, for example
``bds`` -> ``servers/bds/server.toml``. Machine profiles are always paths.

A server file holds ``schema = 1``, an optional ``server`` that must match its folder, and three tables:
``[artifacts]`` (download pins), ``[features]`` (declared parity) and ``[config]`` (the values written into the
server's config files, read by its adapter).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import tomllib
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from bedrock_bench.model import (
    ARTIFACT_SOURCES,
    FEATURE_KEYS,
    PRIORITIES,
    Arm,
    ArtifactCatalog,
    ArtifactSpec,
    ArtifactVersion,
    Benchmark,
    FeatureCell,
    FeatureClass,
    FeatureFile,
    FeatureRange,
    Fidelity,
    Gates,
    Machine,
    OverrideCategory,
    sha256_json,
    to_json,
)

ENV_ROOT = "BBENCH_ROOT"

#: JSON schemas of the result files, shipped as package data.
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

#: Where each input kind that resolves by name lives, relative to the project root.
CONFIG_PATHS = {
    "servers": "servers/{}/server.toml",
}
_SINGULAR = {
    "servers": "server file",
}

SERVER_FILE = "server.toml"
SERVER_TABLES = ("artifacts", "features", "config", "benchmark")
#: JDK and PHP pins shared by the standalone servers, an ``[artifacts]`` table like a server file's.
RUNTIMES_FILE = "servers/runtimes.toml"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SERVER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
METRIC_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PLATFORM_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
HEAP_RE = re.compile(r"^[1-9][0-9]*[MG]$")
PYTHON_RE = re.compile(r"^3\.[0-9]+$")

_MISSING: Any = object()


class ConfigError(ValueError):
    """An input file is missing, unreadable or invalid."""

    def __init__(self, message: str, *, file: Path | str | None = None, key: str | None = None) -> None:
        self.message = message
        self.file = file
        self.key = key
        super().__init__(message)

    def __str__(self) -> str:
        parts = []
        if self.file is not None:
            parts.append(_display_path(self.file))
        if self.key:
            parts.append(self.key)
        parts.append(self.message)
        return ": ".join(parts)


# --------------------------------------------------------------------------------------------------------------------
# Locating inputs
# --------------------------------------------------------------------------------------------------------------------


def project_root() -> Path:
    """The checkout that holds ``configs/`` and the other harness inputs.

    ``$BBENCH_ROOT`` wins; otherwise the checkout this package was imported from; otherwise the working directory.
    """
    override = os.environ.get(ENV_ROOT)
    if override:
        return Path(override).expanduser().resolve()
    candidate = Path(__file__).resolve().parents[1]
    if (candidate / "pyproject.toml").is_file() and (candidate / "configs").is_dir():
        return candidate
    return Path.cwd()


def resolve(kind: str, ref: str | Path) -> Path:
    """Resolve a config reference: a path (anything with a directory part or a ``.toml`` suffix) or a bare name."""
    if kind not in CONFIG_PATHS:
        raise ValueError(f"unknown config kind {kind!r}")
    path = Path(ref)
    if path.suffix != ".toml" and len(path.parts) == 1:
        path = project_root() / CONFIG_PATHS[kind].format(ref)
    if not path.is_file():
        names = available(kind)
        hint = f" (available: {', '.join(names)})" if names else ""
        raise ConfigError(f"no such {_SINGULAR[kind]}{hint}", file=path)
    return path


def available(kind: str) -> list[str]:
    """Names of the inputs of one kind in the project checkout."""
    return sorted(_input_name(p) for p in project_root().glob(CONFIG_PATHS[kind].format("*")) if p.is_file())


def _input_name(path: Path) -> str:
    """The name an input goes by: the folder of a ``server.toml``, otherwise the file name without its suffix."""
    return path.parent.name if path.name == SERVER_FILE else path.stem


def input_file(ref: str | Path, what: str) -> Path:
    """An input that is always given as a path, such as a machine profile."""
    path = Path(ref)
    if not path.is_file():
        raise ConfigError(f"no such {what}", file=path)
    return path


def schema_path(name: str) -> Path:
    """Path of a JSON schema shipped in the package.

    ``schema_path("run")`` -> ``bedrock_bench/schemas/run.schema.json``.
    """
    filename = name if name.endswith(".json") else f"{name}.schema.json"
    return SCHEMA_DIR / filename


def load_schema(name: str) -> dict[str, Any]:
    with schema_path(name).open("rb") as fh:
        return json.load(fh)


def load_toml(path: Path) -> dict[str, Any]:
    try:
        with Path(path).open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError("file not found", file=path) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}", file=path) from None


def file_sha256(path: Path) -> str:
    """Content hash of a text input with line endings normalised, so Windows and Linux checkouts agree."""
    data = Path(path).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------------------------------------------------
# Small parsers shared with other modules
# --------------------------------------------------------------------------------------------------------------------


def parse_cpu_list(spec: str) -> tuple[int, ...]:
    """``"0-3,8,10-11"`` -> ``(0, 1, 2, 3, 8, 10, 11)``. Raises ValueError on malformed input."""
    cpus: set[int] = set()
    text = spec.replace(" ", "")
    if not text:
        raise ValueError("empty CPU list")
    for part in text.split(","):
        lo_text, sep, hi_text = part.partition("-")
        if not lo_text.isdigit() or (sep and not hi_text.isdigit()):
            raise ValueError(f"bad CPU list element {part!r}")
        lo = int(lo_text)
        hi = int(hi_text) if sep else lo
        if hi < lo:
            raise ValueError(f"descending CPU range {part!r}")
        cpus.update(range(lo, hi + 1))
    return tuple(sorted(cpus))


def parse_server_ref(ref: str) -> tuple[str, str]:
    """``"endstone@0.11.14"`` -> ``("endstone", "0.11.14")``."""
    server, sep, version = ref.partition("@")
    if not sep or not SERVER_RE.match(server) or not VERSION_RE.match(version):
        raise ConfigError(f"expected <server>@<version>, e.g. bds@1.26.52.3, got {ref!r}")
    return server, version


def arm_from_ref(
    ref: str,
    *,
    artifact: Path | None = None,
    bds: str | None = None,
    python: str | None = None,
) -> Arm:
    """An arm for a command line ``server@version``; the tag is its id."""
    server, version = parse_server_ref(ref)
    return Arm(
        id=f"{server}@{version}",
        server=server,
        version=version,
        bds=bds,
        python=python,
        artifact=Path(artifact) if artifact is not None else None,
    )


def validate_order(order: str) -> str:
    """Run order of a session: ``rotate``, ``sequential`` or ``seeded:<non-negative int>``."""
    if order in ("rotate", "sequential"):
        return order
    kind, sep, seed = order.partition(":")
    if kind == "seeded" and sep and seed.isdigit():
        return order
    raise ValueError(f"order must be rotate, sequential or seeded:<int>, got {order!r}")


# --------------------------------------------------------------------------------------------------------------------
# Typed table reader
# --------------------------------------------------------------------------------------------------------------------


class _Invalid(ValueError):
    pass


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a float"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "a table"
    return f"a {type(value).__name__}"


def _coerce(value: Any, kind: Any) -> Any:
    origin = typing.get_origin(kind)
    if origin in (typing.Union, types.UnionType):
        inner = [arg for arg in typing.get_args(kind) if arg is not type(None)]
        if len(inner) != 1:
            raise TypeError(f"unsupported config type {kind!r}")
        return _coerce(value, inner[0])
    if origin is tuple:
        element = typing.get_args(kind)[0]
        if not isinstance(value, list):
            raise _Invalid(f"expected an array, got {_type_name(value)}")
        return tuple(_coerce(item, element) for item in value)
    if kind is bool:
        if isinstance(value, bool):
            return value
        raise _Invalid(f"expected true or false, got {_type_name(value)}")
    if kind is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise _Invalid(f"expected an integer, got {_type_name(value)}")
    if kind is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if not math.isfinite(number):
                raise _Invalid("expected a finite number")
            return number
        raise _Invalid(f"expected a number, got {_type_name(value)}")
    if kind is str:
        if isinstance(value, str):
            return value
        hint = " (quote version numbers)" if isinstance(value, (int, float)) and not isinstance(value, bool) else ""
        raise _Invalid(f"expected a string, got {_type_name(value)}{hint}")
    raise TypeError(f"unsupported config type {kind!r}")


def _check(
    value: Any,
    *,
    choices: tuple[Any, ...] | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    above: float | None = None,
    pattern: str | None = None,
    check: Callable[[Any], None] | None = None,
) -> None:
    items = value if isinstance(value, tuple) else (value,)
    for item in items:
        if choices is not None and item not in choices:
            raise _Invalid(f"must be one of {', '.join(map(str, choices))}, got {item!r}")
        if minimum is not None and item < minimum:
            raise _Invalid(f"must be >= {minimum:g}, got {item!r}")
        if maximum is not None and item > maximum:
            raise _Invalid(f"must be <= {maximum:g}, got {item!r}")
        if above is not None and item <= above:
            raise _Invalid(f"must be > {above:g}, got {item!r}")
        if pattern is not None and not re.match(pattern, item):
            raise _Invalid(f"invalid value {item!r}")
    if check is not None:
        try:
            check(value)
        except ValueError as exc:
            raise _Invalid(str(exc)) from None


def _quote_key(key: str) -> str:
    return key if re.match(r"^[A-Za-z0-9_-]+$", key) else json.dumps(key)


class _Table:
    """Typed reads from one TOML table. ``finish()`` rejects every key that was not read."""

    def __init__(self, data: Any, *, file: Path | None, where: str = "") -> None:
        if not isinstance(data, dict):
            raise ConfigError(f"expected a table, got {_type_name(data)}", file=file, key=where or None)
        self.data: dict[str, Any] = data
        self.file = file
        self.where = where
        self._read: set[str] = set()

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def path(self, key: str) -> str:
        return f"{self.where}.{_quote_key(key)}" if self.where else _quote_key(key)

    def error(self, key: str | None, message: str) -> ConfigError:
        return ConfigError(message, file=self.file, key=self.path(key) if key else (self.where or None))

    def get(self, key: str, kind: Any, default: Any = _MISSING, **rules: Any) -> Any:
        self._read.add(key)
        if key not in self.data:
            if default is _MISSING:
                raise self.error(key, "is required")
            return default
        try:
            value = _coerce(self.data[key], kind)
            _check(value, **rules)
        except _Invalid as exc:
            raise self.error(key, str(exc)) from None
        return value

    def raw(self, key: str, default: Any = _MISSING) -> Any:
        """The untyped value, for arrays of tables that the caller validates itself."""
        self._read.add(key)
        if key not in self.data:
            if default is _MISSING:
                raise self.error(key, "is required")
            return default
        return self.data[key]

    def items(self) -> list[tuple[str, Any]]:
        """Every key and value, all marked as read; for tables whose keys are data (versions, ranges)."""
        self._read.update(self.data)
        return list(self.data.items())

    def table(self, key: str, *, required: bool = False) -> _Table:
        """A sub-table; an absent one reads as empty so its defaults apply."""
        self._read.add(key)
        if key not in self.data:
            if required:
                raise self.error(key, "is required")
            return _Table({}, file=self.file, where=self.path(key))
        return _Table(self.data[key], file=self.file, where=self.path(key))

    def sub(self, key: str, value: Any) -> _Table:
        """Wrap a value taken from :meth:`items` as a table located under this one."""
        return _Table(value, file=self.file, where=self.path(key))

    def finish(self, explain: Callable[[str], str | None] | None = None) -> None:
        for key in self.data:
            if key not in self._read:
                message = explain(key) if explain else None
                if message is None:
                    allowed = ", ".join(sorted(self._read)) or "none"
                    message = f"unknown key (allowed here: {allowed})"
                raise self.error(key, message)


def _field_default(f: dataclasses.Field[Any]) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:
        return f.default_factory()
    return _MISSING


def _read_dataclass(table: _Table, cls: type, rules: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Read every field of a model dataclass from a table, with the model's defaults."""
    hints = typing.get_type_hints(cls)
    values = {}
    for f in dataclasses.fields(cls):
        values[f.name] = table.get(f.name, hints[f.name], _field_default(f), **rules.get(f.name, {}))
    table.finish()
    return values


# --------------------------------------------------------------------------------------------------------------------
# Machine profiles
# --------------------------------------------------------------------------------------------------------------------


def load_machine(ref: str | Path | None) -> Machine:
    """Load a machine profile from a path; ``None`` gives the built-in defaults with ``auto`` CPU sets."""
    if ref is None:
        return default_machine()
    path = input_file(ref, "machine profile")
    return parse_machine(load_toml(path), file=path, label=path.stem)


def default_machine() -> Machine:
    return parse_machine({}, label="default")


def parse_machine(data: Mapping[str, Any], *, file: Path | None = None, label: str = "") -> Machine:
    top = _Table(dict(data), file=file)
    label = top.get("label", str, label)
    placement = top.table("placement")
    server_cpus = placement.get("server_cpus", str, "auto", check=_cpu_spec(("auto",)))
    bot_cpus = placement.get("bot_cpus", str, "auto", check=_cpu_spec(("auto",)))
    harness_cpus = placement.get("harness_cpus", str, "bot", check=_cpu_spec(("bot",)))
    priority = placement.get("priority", str, "normal", choices=PRIORITIES)
    placement.finish()
    runtime = top.table("runtime")
    port = runtime.get("port", int, 19672, minimum=1024, maximum=65534)
    jvm_heap = runtime.get("jvm_heap", str, "8G", pattern=HEAP_RE.pattern)
    runtime.finish()
    gate_rules = {f.name: {"minimum": 0} for f in dataclasses.fields(Gates)}
    gates = Gates(**_read_dataclass(top.table("gates"), Gates, gate_rules))
    top.finish()

    if server_cpus != "auto" and bot_cpus != "auto":
        shared = set(parse_cpu_list(server_cpus)) & set(parse_cpu_list(bot_cpus))
        if shared:
            raise top.error("placement", f"server_cpus and bot_cpus share CPUs {sorted(shared)}")
    if harness_cpus not in ("bot",) and server_cpus != "auto":
        shared = set(parse_cpu_list(server_cpus)) & set(parse_cpu_list(harness_cpus))
        if shared:
            raise top.error("placement", f"harness_cpus and server_cpus share CPUs {sorted(shared)}")

    return Machine(
        server_cpus=server_cpus,
        bot_cpus=bot_cpus,
        harness_cpus=harness_cpus,
        priority=priority,
        port=port,
        jvm_heap=jvm_heap,
        gates=gates,
        profile_sha256=_profile_sha256(server_cpus, bot_cpus, harness_cpus, priority, jvm_heap, gates),
        label=label,
    )


def _profile_sha256(
    server_cpus: str, bot_cpus: str, harness_cpus: str, priority: str, jvm_heap: str, gates: Gates
) -> str:
    """Hash of everything in a profile that can change a measurement. Label and port are left out."""
    return sha256_json(
        {
            "placement": {
                "server_cpus": server_cpus,
                "bot_cpus": bot_cpus,
                "harness_cpus": harness_cpus,
                "priority": priority,
            },
            "runtime": {"jvm_heap": jvm_heap},
            "gates": to_json(gates),
        }
    )


def _cpu_spec(keywords: tuple[str, ...]) -> Callable[[str], None]:
    def check(value: str) -> None:
        if value not in keywords:
            parse_cpu_list(value)

    return check


# --------------------------------------------------------------------------------------------------------------------
# Server files
# --------------------------------------------------------------------------------------------------------------------


def load_server_table(ref: str | Path, table: str) -> tuple[Path, dict[str, Any]]:
    """One top-level table of ``servers/<ref>/server.toml``, or of a server file given by path.

    The rest of the file is checked too: ``schema = 1``, ``server`` (when present) equal to the folder name, and no
    keys besides the server tables. A missing table is an error.
    """
    if table not in SERVER_TABLES:
        raise ValueError(f"unknown server table {table!r}")
    path = resolve("servers", ref)
    top = _Table(load_toml(path), file=path)
    top.get("schema", int, check=_equals(1, "only schema = 1 is supported"))
    name = _input_name(path)
    if top.get("server", str, name) != name:
        where = "folder" if path.name == SERVER_FILE else "file"
        raise top.error("server", f"must match the {where} name {name!r}")
    tables = {key: top.table(key, required=key == table) for key in SERVER_TABLES}
    top.finish()
    return path, tables[table].data


def load_benchmark(ref: str | Path) -> Benchmark:
    """The ``[benchmark]`` table of a server file: the default version, and whether ``bbench run`` includes it."""
    path, data = load_server_table(ref, "benchmark")
    table = _Table(data, file=path, where="benchmark")
    version = table.get("version", str, pattern=VERSION_RE.pattern)
    bds = table.get("bds", str, None, pattern=VERSION_RE.pattern)
    python = table.get("python", str, None, pattern=PYTHON_RE.pattern)
    include = table.get("include", bool, True)
    reason = table.get("reason", str, None, pattern=r"^\S")
    table.finish()
    if include == (reason is not None):
        raise table.error("reason", "is required with include = false, and only then")
    return Benchmark(_input_name(path), version, bds=bds, python=python, include=include, reason=reason)


def benchmark_defaults() -> list[Benchmark]:
    """The ``[benchmark]`` table of every server folder in servers/, by name."""
    return [load_benchmark(name) for name in available("servers")]


# --------------------------------------------------------------------------------------------------------------------
# Config values
# --------------------------------------------------------------------------------------------------------------------

#: File formats a ``[config.file."<path>"]`` table may name; each adapter family accepts a subset.
CONFIG_FORMATS = ("properties", "yaml", "toml")


@dataclass(frozen=True)
class ConfigEntry:
    """One ``key = { value, reason, ... }`` entry of a server file's ``[config]`` table, checked by the adapter."""

    file: str
    category: OverrideCategory
    key: str
    entry: Any
    where: str


def load_config_table(ref: str | Path) -> tuple[Path, dict[str, str], tuple[ConfigEntry, ...]]:
    """The ``[config]`` table of a server file: the format of each file and every entry, in file order."""
    path, data = load_server_table(ref, "config")
    formats, entries = parse_config_table(data, file=path)
    return path, formats, entries


def parse_config_table(
    data: Mapping[str, Any], *, file: Path | None = None
) -> tuple[dict[str, str], tuple[ConfigEntry, ...]]:
    """``[config.file."<path>"]`` tables, each with a ``format`` and one sub-table per override category."""
    unknown = sorted(set(data) - {"file"})
    if unknown:
        raise ConfigError(f"unknown keys {unknown}", file=file, key="config")
    files = data.get("file")
    if not isinstance(files, dict) or not files:
        raise ConfigError('needs at least one [config.file."<path>"] table', file=file, key="config.file")
    formats: dict[str, str] = {}
    entries: list[ConfigEntry] = []
    for name, table in files.items():
        where = f'config.file."{name}"'
        if not isinstance(table, dict):
            raise ConfigError("must be a table", file=file, key=where)
        if not name or name.startswith(("/", "\\")) or ".." in PurePosixPath(name).parts:
            raise ConfigError("must be a relative path inside the server directory", file=file, key=where)
        fmt = table.get("format")
        if fmt not in CONFIG_FORMATS:
            raise ConfigError(f"format must be one of {', '.join(CONFIG_FORMATS)}", file=file, key=f"{where}.format")
        formats[name] = fmt
        for section, keys in table.items():
            if section == "format":
                continue
            try:
                category = OverrideCategory(section)
            except ValueError:
                raise ConfigError(
                    f"unknown table; use format or one of {', '.join(c.value for c in OverrideCategory)}",
                    file=file,
                    key=f"{where}.{section}",
                ) from None
            if not isinstance(keys, dict):
                raise ConfigError("must be a table", file=file, key=f"{where}.{section}")
            entries.extend(
                ConfigEntry(name, category, key, entry, f"{where}.{section}.{key}") for key, entry in keys.items()
            )
    return formats, tuple(entries)


def single_config_file(path: Path, formats: Mapping[str, str], name: str, fmt: str) -> None:
    """Refuse a ``[config]`` table that names any file but ``name`` in format ``fmt``."""
    if dict(formats) != {name: fmt}:
        raise ConfigError(
            f'needs exactly one [config.file."{name}"] table, with format = "{fmt}"', file=path, key="config.file"
        )


# --------------------------------------------------------------------------------------------------------------------
# Artifact pins
# --------------------------------------------------------------------------------------------------------------------

_ARTIFACT_KEYS = ("name", "url", "sha256", "size", "executables", "eula", "source")


def load_artifacts(ref: str | Path) -> ArtifactCatalog:
    """The ``[artifacts]`` table of a server file, or of :data:`RUNTIMES_FILE` given by path."""
    path, table = load_server_table(ref, "artifacts")
    return parse_artifacts(table, file=path, server=_input_name(path), where="artifacts")


def parse_artifacts(
    data: Mapping[str, Any], *, file: Path | None = None, server: str, where: str = ""
) -> ArtifactCatalog:
    """An ``[artifacts]`` table: ``[version."<v>".<platform>]`` tables with url, sha256 and optional keys.

    Non-table keys of a version table land in ``ArtifactVersion.meta``; unknown keys of a platform table land in
    ``ArtifactSpec.extra``. Both are for the adapter to interpret.
    """
    top = _Table(dict(data), file=file, where=where)
    source = top.get("source", str, "web", choices=ARTIFACT_SOURCES)
    versions_table = top.table("version", required=True)
    versions: dict[str, ArtifactVersion] = {}
    for version, raw in versions_table.items():
        if not VERSION_RE.match(version):
            raise versions_table.error(version, "is not a valid version")
        versions[version] = _read_artifact_version(versions_table.sub(version, raw), server, version, source)
    if not versions:
        raise versions_table.error(None, "pins no versions")
    top.finish()
    return ArtifactCatalog(
        server=server,
        path=file if file is not None else Path(f"{server}.toml"),
        sha256=sha256_json(data),
        versions=versions,
    )


def _read_artifact_version(table: _Table, server: str, version: str, source: str) -> ArtifactVersion:
    platforms: dict[str, ArtifactSpec] = {}
    meta: dict[str, Any] = {}
    for key, value in table.items():
        if isinstance(value, dict):
            if not PLATFORM_KEY_RE.match(key):
                raise table.error(key, "is not a valid platform key")
            platforms[key] = _read_artifact(table.sub(key, value), server, version, key, source)
        else:
            meta[key] = value
    if not platforms:
        raise table.error(None, 'has no platform tables (e.g. [version."X".windows])')
    return ArtifactVersion(version=version, platforms=platforms, meta=meta)


def _read_artifact(table: _Table, server: str, version: str, platform: str, source: str) -> ArtifactSpec:
    url = table.get("url", str, pattern=r"^https://\S+$")
    sha256 = table.get("sha256", str, "", check=_sha256_or_empty).lower()
    size = table.get("size", int, 0, minimum=0)
    name = table.get("name", str, url.rsplit("/", 1)[-1].split("?", 1)[0] or f"{server}-{version}-{platform}")
    executables = table.get("executables", tuple[str, ...], (), check=_relative_paths)
    eula = table.get("eula", str, None, pattern=r"^[a-z][a-z0-9_-]*$")
    source = table.get("source", str, source, choices=ARTIFACT_SOURCES)
    extra = {key: value for key, value in table.data.items() if key not in _ARTIFACT_KEYS}
    return ArtifactSpec(
        name=name,
        url=url,
        sha256=sha256 or None,
        size=size or None,
        executables=executables,
        eula=eula,
        source=source,
        extra=extra,
    )


def _sha256_or_empty(value: str) -> None:
    if value and not SHA256_RE.match(value.lower()):
        raise ValueError("must be a sha256 hex digest, or empty until pinned")


def _relative_paths(paths: tuple[str, ...]) -> None:
    for item in paths:
        if not item or item.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", item) or ".." in Path(item).parts:
            raise ValueError(f"must be a relative path inside the archive, got {item!r}")


# --------------------------------------------------------------------------------------------------------------------
# Feature parity files
# --------------------------------------------------------------------------------------------------------------------


def load_features(ref: str | Path) -> FeatureFile:
    """The ``[features]`` table of a server file."""
    path, table = load_server_table(ref, "features")
    return parse_features(table, file=path, server=_input_name(path), where="features")


def parse_features(data: Mapping[str, Any], *, file: Path | None = None, server: str, where: str = "") -> FeatureFile:
    """A ``[features]`` table, hashed alone so that edits elsewhere in the server file keep recorded parity valid."""
    top = _Table(dict(data), file=file, where=where)
    ranges_table = top.table("range", required=True)
    ranges = []
    for prefix, raw in ranges_table.items():
        if prefix != "*" and not VERSION_RE.match(prefix):
            raise ranges_table.error(prefix, "is not a version prefix")
        ranges.append(_read_feature_range(ranges_table.sub(prefix, raw), prefix))
    if not ranges:
        raise ranges_table.error(None, "declares no version ranges")
    top.finish()
    return FeatureFile(
        server=server,
        path=file if file is not None else Path(f"{server}.toml"),
        sha256=sha256_json(data),
        ranges=tuple(ranges),
    )


def _read_feature_range(table: _Table, prefix: str) -> FeatureRange:
    fidelity = Fidelity(table.get("fidelity", str, choices=tuple(Fidelity)))
    cells: dict[str, FeatureCell] = {}
    for key, raw in table.items():
        if key == "fidelity":
            continue
        if key not in FEATURE_KEYS:
            raise table.error(key, "unknown feature key (see docs/methodology.md#fairness-and-feature-parity)")
        cell = table.sub(key, raw)
        cls = FeatureClass(cell.get("class", str, choices=tuple(FeatureClass)))
        evidence = cell.get("evidence", str, None, pattern=r"^\S.*$")
        observed_by = cell.get("observed_by", str, None, pattern=METRIC_ID_RE.pattern)
        note = cell.get("note", str, None)
        cell.finish()
        if cls is not FeatureClass.UNKNOWN and evidence is None and observed_by is None:
            raise cell.error(None, "needs an evidence link or an observed_by metric unless the class is unknown")
        cells[key] = FeatureCell(key=key, cls=cls, evidence=evidence, observed_by=observed_by, note=note)
    table.finish()
    return FeatureRange(prefix=prefix, fidelity=fidelity, cells=cells)


def _equals(expected: Any, message: str) -> Callable[[Any], None]:
    def check(value: Any) -> None:
        if value != expected:
            raise ValueError(message)

    return check


# --------------------------------------------------------------------------------------------------------------------
# Benchmark file
# --------------------------------------------------------------------------------------------------------------------

#: The setup shared by every server.
BENCHMARK_FILE = "configs/benchmark.toml"


@dataclass(frozen=True)
class ReportMetric:
    """One core metric of the report: its column, where its value comes from, and how it is shown."""

    column: str  # key in summary.json, column prefix in summary.csv
    metric: str  # metrics.json id
    title: str  # report.md header and chart title, with the unit
    stat: str | None = None  # key of a distribution value
    decimals: int | None = None  # None: three significant digits


def load_report_metrics(path: Path | None = None) -> tuple[ReportMetric, ...]:
    """The core metrics in the ``[report]`` table of configs/benchmark.toml, in report order."""
    path = path or project_root() / BENCHMARK_FILE
    report = _Table(load_toml(path), file=path).table("report", required=True)
    entries = report.raw("metrics")
    report.finish()
    if not isinstance(entries, list) or not entries:
        raise report.error("metrics", "expected a non-empty array of tables")
    rules = {
        "column": {"pattern": METRIC_ID_RE.pattern},
        "metric": {"pattern": METRIC_ID_RE.pattern},
        "stat": {"pattern": METRIC_ID_RE.pattern},
        "decimals": {"minimum": 0},
    }
    metrics = tuple(
        ReportMetric(**_read_dataclass(_Table(entry, file=path, where=f"report.metrics[{i}]"), ReportMetric, rules))
        for i, entry in enumerate(entries)
    )
    columns = [m.column for m in metrics]
    for column in columns:
        if columns.count(column) > 1:
            raise report.error("metrics", f"column {column!r} appears twice")
    return metrics


# --------------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------------


def _display_path(path: Path | str) -> str:
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except (ValueError, OSError):
        return candidate.as_posix()
