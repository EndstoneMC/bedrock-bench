# SPDX-License-Identifier: Apache-2.0
"""Adapter registry.

Adapters are found, not listed: every module in this package and every object published under the
``bedrock_bench.adapters`` entry point group is scanned for concrete :class:`ServerAdapter` subclasses that declare
their own ``name``. Adding a server therefore never touches this file. A third-party package registers with::

    [project.entry-points."bedrock_bench.adapters"]
    myserver = "myserver_bench.adapter"      # a module, or "myserver_bench.adapter:MyServerAdapter" for one class
"""

from __future__ import annotations

import functools
import importlib
import importlib.metadata
import inspect
import pkgutil
from collections.abc import Iterator
from types import ModuleType

from bedrock_bench.adapters.base import ServerAdapter
from bedrock_bench.config import ConfigError
from bedrock_bench.model import Arm, Machine

ENTRY_POINT_GROUP = "bedrock_bench.adapters"

#: Modules of this package that hold shared code rather than adapters.
_SHARED_MODULES = frozenset({"base", "bdsfamily"})


class AdapterError(RuntimeError):
    """An adapter module failed to import or breaks the contract."""


def names() -> list[str]:
    """Registered server names, sorted."""
    return sorted(_registry())


def all_adapters() -> dict[str, type[ServerAdapter]]:
    return dict(_registry())


def get(name: str) -> type[ServerAdapter]:
    registry = _registry()
    try:
        return registry[name]
    except KeyError:
        known = ", ".join(sorted(registry)) or "none"
        raise ConfigError(f"unknown server {name!r} (known: {known})") from None


def create(arm: Arm, machine: Machine) -> ServerAdapter:
    """An adapter instance for one arm."""
    return get(arm.server)(arm, machine)


def refresh() -> None:
    """Forget the discovered adapters, e.g. after a test registered a new one."""
    _registry.cache_clear()


@functools.cache
def _registry() -> dict[str, type[ServerAdapter]]:
    found: dict[str, type[ServerAdapter]] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_") or info.name in _SHARED_MODULES:
            continue
        qualified = f"{__name__}.{info.name}"
        try:
            module = importlib.import_module(qualified)
        except Exception as exc:
            raise AdapterError(f"cannot import adapter module {qualified}: {exc}") from exc
        for cls in _adapters_in(module):
            _register(found, cls, qualified)
    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            target = entry_point.load()
        except Exception as exc:
            raise AdapterError(f"cannot load adapter entry point {entry_point.value}: {exc}") from exc
        if inspect.isclass(target):
            classes = [target]
        elif inspect.ismodule(target):
            classes = list(_adapters_in(target))
        else:
            raise AdapterError(f"adapter entry point {entry_point.value} is neither a module nor a class")
        for cls in classes:
            _register(found, cls, entry_point.value)
    return found


def _adapters_in(module: ModuleType) -> Iterator[type[ServerAdapter]]:
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if (
            obj.__module__ == module.__name__
            and issubclass(obj, ServerAdapter)
            and not inspect.isabstract(obj)
            and "name" in obj.__dict__
        ):
            yield obj


def _register(found: dict[str, type[ServerAdapter]], cls: type[ServerAdapter], origin: str) -> None:
    if not (inspect.isclass(cls) and issubclass(cls, ServerAdapter)) or inspect.isabstract(cls):
        raise AdapterError(f"{origin}: {cls!r} is not a concrete ServerAdapter")
    problems = cls.class_problems()
    if problems:
        raise AdapterError(f"{origin}: {cls.__name__} breaks the adapter contract: " + "; ".join(problems))
    previous = found.get(cls.name)
    if previous is not None and previous is not cls:
        raise AdapterError(f"server name {cls.name!r} is claimed by {previous.__module__} and {origin}")
    found[cls.name] = cls


__all__ = ["ENTRY_POINT_GROUP", "AdapterError", "ServerAdapter", "all_adapters", "create", "get", "names", "refresh"]
