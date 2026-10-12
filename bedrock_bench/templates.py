# SPDX-License-Identifier: Apache-2.0
"""Render a server's config templates and merge them onto the files in its server directory.

A template is a file in ``servers/<server>/`` named like the config file it produces, in that file's own format.
Each adapter names the templates it writes. A template is rendered with Jinja (``StrictUndefined``, no autoescaping)
and merged onto its target by the target's format: properties set or add keys, YAML, TOML and JSON deep merge. Lists
and empty mappings replace. A target that does not exist yet gets the rendered text as is. Every key the template sets
is recorded with its value before and after, and every written file with its sha256.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import jinja2

from bedrock_bench.config import ConfigError
from bedrock_bench.model import ConfigFile, KeyChange

FORMATS = {".properties": "properties", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".json": "json"}


def file_format(path: str) -> str:
    """``properties``, ``yaml``, ``toml`` or ``json``, from the file name."""
    fmt = FORMATS.get(PurePosixPath(path).suffix.lower())
    if fmt is None:
        raise ConfigError(f"{path} has no known config format ({', '.join(FORMATS)})")
    return fmt


# --------------------------------------------------------------------------------------------------------------------
# Rendering and merging
# --------------------------------------------------------------------------------------------------------------------


def render(folder: Path, name: str, variables: Mapping[str, Any]) -> str:
    """Render ``folder/name`` with ``variables``. An undefined variable is an error."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(folder),
        undefined=jinja2.StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    try:
        return env.get_template(name).render(variables)
    except jinja2.TemplateError as exc:
        raise ConfigError(f"{folder / name}: {exc}") from exc


def apply(
    folder: Path, name: str, server_dir: Path, variables: Mapping[str, Any], *, target: str | None = None
) -> ConfigFile:
    """Render ``folder/name`` and merge it onto ``server_dir/target``; ``target`` defaults to ``name``."""
    target = target or name
    fmt = file_format(target)
    rendered = render(folder, name, variables)
    path = server_dir / target
    existing = path.read_bytes().decode("utf-8-sig") if path.is_file() else None
    try:
        text, keys = merge(fmt, existing, rendered)
    except ConfigError as exc:
        raise ConfigError(f"{folder / name} -> {target}: {exc}") from exc
    data = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return ConfigFile(target, hashlib.sha256(data).hexdigest(), name, existing is None, tuple(keys))


def readback(server_dir: Path, files: Iterable[ConfigFile]) -> list[str]:
    """Written values that are no longer in their file; empty when every key reads back."""
    problems = []
    for written in files:
        if not written.keys:
            continue
        path = server_dir / written.path
        if not path.is_file():
            problems.append(f"{written.path} is missing after the run")
            continue
        found = values(file_format(written.path), path.read_bytes().decode("utf-8-sig"))
        for change in written.keys:
            value = found.get(change.key)
            if value != change.after:
                problems.append(
                    f"{written.path}: {change.key} was written as {change.after!r} and read back as {value!r}"
                )
    return problems


def values(fmt: str, text: str) -> dict[str, Any]:
    """Every key in a config file's text, named and valued the way :func:`merge` reports them."""
    if fmt == "properties":
        return {key: value for _, key, value in _properties(text, strict=False)}
    if fmt == "yaml":
        return {e.path: _yaml_scalar(e.value) for e in _yaml_entries(text, strict=False) if e.value}
    data = _parse_toml(text, "the file") if fmt == "toml" else _parse_json(text, "the file")
    return {".".join(path): value for path, value in _leaves(data)}


def merge(fmt: str, existing: str | None, rendered: str) -> tuple[str, list[KeyChange]]:
    """Merge rendered template text onto a file's text (``None`` when absent): the new text and every key set."""
    merger = {"properties": _merge_properties, "yaml": _merge_yaml, "toml": _merge_toml, "json": _merge_json}[fmt]
    text, keys = merger(existing if existing is not None else "", rendered)
    return (rendered if existing is None else text), keys


# --------------------------------------------------------------------------------------------------------------------
# properties
# --------------------------------------------------------------------------------------------------------------------


def _properties(text: str, *, strict: bool) -> Iterator[tuple[int, str, str]]:
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!")):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            if strict:
                raise ConfigError(f"line {index + 1}: expected key=value, got {line!r}")
            continue
        yield index, key.strip(), value.strip()


def _merge_properties(existing: str, rendered: str) -> tuple[str, list[KeyChange]]:
    newline = "\r\n" if "\r\n" in existing else "\n"
    lines = existing.splitlines()
    found = {key: (index, value) for index, key, value in _properties(existing, strict=False)}
    keys = []
    for _, key, value in _properties(rendered, strict=True):
        index, before = found.get(key, (None, None))
        if index is None:
            lines.append(f"{key}={value}")
            found[key] = (len(lines) - 1, value)
        else:
            lines[index] = f"{key}={value}"
        keys.append(KeyChange(key, before, value))
    return newline.join(lines) + newline, keys


# --------------------------------------------------------------------------------------------------------------------
# YAML: block mappings of scalars, edited line by line so comments and everything else stay
# --------------------------------------------------------------------------------------------------------------------

_YAML_ENTRY = re.compile(
    r"^(?P<indent> *)(?P<key>[A-Za-z0-9_][A-Za-z0-9_.-]*|\"[^\"]*\"|'[^']*') *:(?: +(?P<value>.*))?$"
)


@dataclass(frozen=True)
class _YamlLine:
    index: int
    indent: int
    path: str
    value: str  # raw text after the colon without a trailing comment; empty for a mapping


def _yaml_entries(text: str, *, strict: bool) -> list[_YamlLine]:
    entries: list[_YamlLine] = []
    stack: list[tuple[int, str]] = []
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        match = _YAML_ENTRY.match(line.rstrip())
        if match is None:
            if strict:
                raise ConfigError(f"line {index + 1}: only block mappings of 'key: value' lines can be merged")
            continue
        indent = len(match["indent"])
        key = match["key"].strip("\"'")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        entry = _YamlLine(index, indent, ".".join([*(k for _, k in stack), key]), _yaml_strip_comment(match["value"]))
        entries.append(entry)
        if not entry.value:
            stack.append((indent, key))
    return entries


def _yaml_strip_comment(value: str | None) -> str:
    value = (value or "").strip()
    quote = None
    for i, ch in enumerate(value):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1] in " \t"):
            return value[:i].rstrip()
    return value


def _yaml_scalar(value: str) -> str:
    if len(value) < 2 or value[0] != value[-1] or value[0] not in "\"'":
        return value
    if value[0] == "'":
        return value[1:-1].replace("''", "'")
    try:
        return json.loads(value)
    except ValueError:
        return value[1:-1]


def _yaml_block_end(lines: list[str], entry: _YamlLine) -> int:
    end = last = entry.index + 1
    while end < len(lines):
        line = lines[end]
        if line.strip():
            if len(line) - len(line.lstrip(" ")) <= entry.indent:
                break
            last = end + 1
        end += 1
    return last


def _yaml_set(lines: list[str], path: str, value: str) -> str | None:
    """Set ``path`` to the raw ``value`` in ``lines``; returns the scalar it had, ``None`` when absent."""
    entries = _yaml_entries("\n".join(lines), strict=False)
    by_path = {e.path: e for e in entries}
    parts = path.split(".")
    if (target := by_path.get(path)) is not None:
        end = target.index + 1 if target.value else _yaml_block_end(lines, target)
        lines[target.index : end] = [f"{' ' * target.indent}{parts[-1]}: {value}"]
        return _yaml_scalar(target.value) if target.value else None
    depth = next((d for d in range(len(parts) - 1, 0, -1) if ".".join(parts[:d]) in by_path), 0)
    if depth:
        parent = by_path[".".join(parts[:depth])]
        if parent.value:
            raise ConfigError(f"cannot set {path}: {parent.path} is a value, not a mapping")
        child = next((e for e in entries if e.index > parent.index and e.path.startswith(parent.path + ".")), None)
        unit = (child.indent - parent.indent) if child else 2
        at, indent = _yaml_block_end(lines, parent), parent.indent + unit
    else:
        unit = min((e.indent for e in entries if e.indent > 0), default=2)
        while lines and not lines[-1].strip():
            lines.pop()
        at, indent = len(lines), 0
    rest = parts[depth:]
    new = [f"{' ' * (indent + i * unit)}{key}:" for i, key in enumerate(rest[:-1])]
    new.append(f"{' ' * (indent + (len(rest) - 1) * unit)}{rest[-1]}: {value}")
    lines[at:at] = new
    return None


def _merge_yaml(existing: str, rendered: str) -> tuple[str, list[KeyChange]]:
    newline = "\r\n" if "\r\n" in existing else "\n"
    lines = existing.splitlines()
    keys = []
    for entry in _yaml_entries(rendered, strict=True):
        if entry.value:
            before = _yaml_set(lines, entry.path, entry.value)
            keys.append(KeyChange(entry.path, before, _yaml_scalar(entry.value)))
    return newline.join(lines) + newline, keys


# --------------------------------------------------------------------------------------------------------------------
# TOML and JSON: parsed, deep merged and written back
# --------------------------------------------------------------------------------------------------------------------


def _leaves(data: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    for key, value in data.items():
        if isinstance(value, Mapping) and value:
            yield from _leaves(value, (*prefix, key))
        else:
            yield (*prefix, key), value


def _deep_merge(base: dict[str, Any], over: Mapping[str, Any]) -> list[KeyChange]:
    keys = []
    for path, value in _leaves(over):
        node = base
        for depth, part in enumerate(path[:-1]):
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ConfigError(f"cannot set {'.'.join(path)}: {'.'.join(path[: depth + 1])} is a value")
            node = child
        keys.append(KeyChange(".".join(path), node.get(path[-1]), value))
        node[path[-1]] = value
    return keys


def _parse_json(text: str, what: str) -> dict[str, Any]:
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{what} must be a JSON object")
    return data


def _merge_json(existing: str, rendered: str) -> tuple[str, list[KeyChange]]:
    data = _parse_json(existing, "the existing file")
    keys = _deep_merge(data, _parse_json(rendered, "the rendered template"))
    return json.dumps(data, indent=4, ensure_ascii=False) + "\n", keys


def _parse_toml(text: str, what: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{what} is not valid TOML: {exc}") from exc


def _merge_toml(existing: str, rendered: str) -> tuple[str, list[KeyChange]]:
    data = _parse_toml(existing, "the existing file")
    keys = _deep_merge(data, _parse_toml(rendered, "the rendered template"))
    return _toml_dump(data), keys


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        return ("inf" if value > 0 else "-inf") if math.isinf(value) else repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + "}"
    raise ConfigError(f"cannot write {type(value).__name__} as TOML")


def _toml_dump(data: Mapping[str, Any]) -> str:
    out: list[str] = []

    def table(node: Mapping[str, Any], path: tuple[str, ...]) -> None:
        values = [(k, v) for k, v in node.items() if not (isinstance(v, Mapping) and v)]
        tables = [(k, v) for k, v in node.items() if isinstance(v, Mapping) and v]
        if path and (values or not tables):
            if out:
                out.append("")
            out.append("[" + ".".join(_toml_key(p) for p in path) + "]")
        out.extend(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in values)
        for key, child in tables:
            table(child, (*path, key))

    table(data, ())
    return "\n".join(out) + "\n" if out else ""
