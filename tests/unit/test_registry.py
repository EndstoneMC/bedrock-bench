# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from bedrock_bench.metrics import compute, registry
from bedrock_bench.metrics.registry import METRICS, TABLES, UNITS, render_docs, render_markdown
from fixtures.runs import synthetic


def test_declarations_are_consistent() -> None:
    assert registry.problems() == []


def test_every_id_carries_its_unit() -> None:
    for metric in METRICS:
        unit = UNITS[metric.unit]
        if unit.tokens:
            words = metric.id.split("_")
            assert any(
                words[i : i + len(t.split("_"))] == t.split("_") for t in unit.tokens for i in range(len(words))
            ), metric.id


@pytest.mark.parametrize(
    ("metric_id", "token", "expected"),
    [
        ("tick_ms_per_s_steady", "ms_per_s", True),
        ("recovery_s", "s", True),
        ("process_cores_load", "cores", True),
        ("tick_cpu_ms_per_tick_load", "ms_per_s", False),
        ("settle_s", "ms", False),
        ("tps_window_steady", "tps", True),
    ],
)
def test_unit_word_matching(metric_id: str, token: str, expected: bool) -> None:
    assert registry._has_words(metric_id, token) is expected


def test_ids_are_valid_json_keys_and_windowed_templates_expand() -> None:
    for metric in METRICS:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", metric.id), metric.id
        if "<W>" in metric.template:
            assert metric.id == metric.template.replace("<W>", metric.window or "")
    assert registry.get("tps_window_steady").direction == "higher"
    assert registry.table("pkt_mix_steady").columns == ("packet", "per_bot_s")
    assert len(registry.ids()) == len(set(registry.ids()))


@pytest.mark.parametrize("name", synthetic.NAMES)
def test_compute_emits_exactly_the_declared_ids(name: str) -> None:
    doc = compute(synthetic.build(name))
    assert list(doc["metrics"]) == registry.ids()
    assert list(doc["tables"]) == [t.id for t in TABLES]
    for metric_id, entry in doc["metrics"].items():
        declared = registry.get(metric_id)
        assert entry["unit"] == declared.unit
        assert entry["window"] == declared.window
        assert entry.get("reason") != "not computed", metric_id
        if entry["availability"] == "measured" and declared.stats:
            assert set(entry["value"]) <= {*declared.stats, "n"}, metric_id
        if entry.get("censored"):
            assert declared.censorable, metric_id
    for table_id, table in doc["tables"].items():
        assert table["columns"] == list(registry.table(table_id).columns)
        assert all(len(row) == len(table["columns"]) for row in table["rows"]), table_id


def test_metrics_md_is_current(project_root: Path) -> None:
    current = (project_root / "docs" / "METRICS.md").read_text(encoding="utf-8")
    assert current == render_markdown(), "docs/METRICS.md is out of date: run 'bbench docs metrics'"


def test_render_docs_writes_and_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BBENCH_ROOT", str(tmp_path))
    assert render_docs(argparse.Namespace(target="metrics", check=True)) == 1
    assert render_docs(argparse.Namespace(target="metrics", check=False)) == 0
    assert (tmp_path / "docs" / "METRICS.md").read_text(encoding="utf-8") == render_markdown()
    assert render_docs(argparse.Namespace(target="metrics", check=True)) == 0
    assert "up to date" in capsys.readouterr().out


def test_markdown_lists_every_template_and_gate() -> None:
    text = render_markdown()
    for metric in METRICS:
        assert f"`{metric.template}`" in text
    for tbl in TABLES:
        assert f"`{tbl.template}`" in text
    assert "`probe_monotonic`" in text and "`joined_all` | server_failed" in text
