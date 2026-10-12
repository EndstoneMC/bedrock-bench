# SPDX-License-Identifier: Apache-2.0
"""Shared pytest fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bedrock_bench.config import project_root as _project_root
from bedrock_bench.config import schema_path

PROJECT_ROOT = _project_root()


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may touch the user's real cache."""
    monkeypatch.setenv("BBENCH_CACHE", str(tmp_path_factory.getbasetemp() / "bbench-cache"))


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def write_file(tmp_path: Path) -> Callable[[str, str], Path]:
    """Write a text file under ``tmp_path`` (LF line endings) and return its path."""

    def write(relative: str, text: str) -> Path:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    return write


@pytest.fixture(scope="session")
def schema_validator() -> Callable[[str], Any]:
    """``schema_validator("run")`` -> a jsonschema validator for the packaged ``run.schema.json``."""
    jsonschema = pytest.importorskip("jsonschema")
    cache: dict[str, Any] = {}

    def get(name: str) -> Any:
        if name not in cache:
            schema = json.loads(schema_path(name).read_text(encoding="utf-8"))
            cls = jsonschema.validators.validator_for(schema)
            cls.check_schema(schema)
            cache[name] = cls(schema, format_checker=cls.FORMAT_CHECKER)
        return cache[name]

    return get
