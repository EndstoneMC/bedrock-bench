# SPDX-License-Identifier: Apache-2.0
"""Footnote texts for flags, overrides, failed runs and missing metrics."""

from __future__ import annotations

import pytest

from bedrock_bench.model import Flag, OverrideCategory
from bedrock_bench.report.footnotes import Footnotes, availability_text, flag_text, override_text, status_text

ARM = {"id": "endstone-0.11.13", "server": "endstone", "version": "0.11.13", "bds": "1.26.52.3"}


class TestFootnotes:
    def test_numbers_in_order_of_first_use_and_shares_equal_texts(self) -> None:
        notes = Footnotes()
        assert notes.ref("first") == "[^1]"
        assert notes.ref("second") == "[^2]"
        assert notes.ref("first") == "[^1]"
        assert notes.refs(["second", "third"]) == "[^2][^3]"
        assert len(notes) == 3
        assert notes.render() == "[^1]: first\n[^2]: second\n[^3]: third"

    def test_empty(self) -> None:
        assert Footnotes().render() == ""


class TestFlagText:
    @pytest.mark.parametrize("flag", list(Flag))
    def test_every_flag_has_its_own_text(self, flag: Flag) -> None:
        text = flag_text(flag.value, count=1, total=1, arm=ARM)
        assert not text.startswith("Flag `") and text.endswith(".")

    def test_run_counts(self) -> None:
        assert flag_text("settle_capped", count=3, total=3, arm=ARM).endswith("slowed down.")
        assert "(in 1 of 3 runs)" in flag_text("settle_capped", count=1, total=3, arm=ARM)

    def test_parameters(self) -> None:
        text = flag_text("known_issue:devtools_spin", count=3, total=3, arm=ARM)
        assert "`devtools_spin`" in text and "docs/servers/endstone.md" in text
        assert "`player.physics`" in flag_text("parity_mismatch:player.physics", count=1, total=1, arm=ARM)
        assert "1.26.52.3" in flag_text("different_bds_build", count=1, total=1, arm=ARM)

    def test_unknown_flags_still_render(self) -> None:
        assert flag_text("new_flag:x", count=2, total=3, arm=ARM) == "Flag `new_flag` `x` (in 2 of 3 runs)."


class TestOverrideText:
    def override(self, category: OverrideCategory, default: str | None = "1024") -> dict[str, str | None]:
        return {
            "category": category.value,
            "file": "pocketmine.yml",
            "key": "memory.main-limit",
            "default": default,
            "value": "0",
            "reason": "the default limit crashes the server at about 23 s.",
        }

    def test_rescue(self) -> None:
        text = override_text(self.override(OverrideCategory.RESCUE))
        assert text == (
            "Rescue override: `pocketmine.yml` `memory.main-limit` = `0` (default `1024`): the default limit crashes "
            "the server at about 23 s. The default config does not survive this workload."
        )

    def test_nonvanilla(self) -> None:
        text = override_text(self.override(OverrideCategory.NONVANILLA, default=None))
        assert text is not None and text.startswith("Non-vanilla override:")
        assert "(not set by default)" in text and text.endswith("It changes gameplay to save work.")

    @pytest.mark.parametrize("category", [OverrideCategory.HARNESS, OverrideCategory.PARITY])
    def test_other_categories_are_not_footnoted(self, category: OverrideCategory) -> None:
        assert override_text(self.override(category)) is None


class TestStatusText:
    def test_all_ok(self) -> None:
        assert status_text([{"status": "ok", "repeat": 1, "reasons": []}]) is None

    def test_failures(self) -> None:
        runs = [
            {"status": "ok", "repeat": 1, "reasons": []},
            {"status": "server_failed", "repeat": 2, "reasons": ["server_alive: exited with code 1"]},
            {"status": "invalid", "repeat": 3, "reasons": []},
        ]
        assert status_text(runs) == (
            "server_failed in 1 of 3 runs (run 2: server_alive: exited with code 1); "
            "invalid in 1 of 3 runs (run 3: no reason recorded). Runs that are not ok are left out of the statistics."
        )


class TestAvailabilityText:
    def test_not_applicable(self) -> None:
        agg = {"availability": "not_applicable", "n": 0, "reasons": ["no tick thread"]}
        assert availability_text("Tick CPU", agg, 3) == "Tick CPU: not applicable (no tick thread)."

    def test_partly_unavailable(self) -> None:
        agg = {"availability": "measured", "n": 2, "reasons": ["sampler died"]}
        assert availability_text("Memory", agg, 3) == "Memory: unavailable in 1 of 3 ok runs (sampler died)."

    def test_fully_measured(self) -> None:
        assert availability_text("Memory", {"availability": "measured", "n": 3}, 3) is None
