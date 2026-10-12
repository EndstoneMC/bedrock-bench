# SPDX-License-Identifier: Apache-2.0
"""Footnote texts for flags, overrides, failed runs and missing metrics.

Footnotes carry everything a reader must know next to a number: why a run failed, which config was changed to make
a server run at all, what a flag means. Rule R6: every ``rescue`` and ``nonvanilla`` override gets one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from bedrock_bench.model import Flag, OverrideCategory, Status

_FLAG_TEXT: dict[str, str] = {
    Flag.SETTLE_CAPPED: "The settle phase hit its time cap before chunk delivery slowed down{runs}.",
    Flag.VIEWS_INCOMPLETE: "Views were still incomplete when the settle phase ended{runs}.",
    Flag.CHAOS_OVERRUN: "Console commands fell behind during chaos and new actions stopped early{runs}.",
    Flag.PARITY_MISMATCH: "The declared parity of `{param}` disagrees with its observed counter{runs}.",
    Flag.BOT_PROTOCOL_DIFFERS: "The bots used a build for a different client protocol than the other arms{runs}.",
    Flag.TERRAIN_MISMATCH: "The world is not built by the vanilla generator, so chunk work is not comparable{runs}.",
    Flag.DIFFERENT_BDS_BUILD: (
        "The matrix has no vanilla run of the BDS build this server hosts ({bds}), so no ratio to vanilla is shown."
    ),
    Flag.KNOWN_ISSUE: "Known issue `{param}` affects this build{runs}; see docs/servers/{server}.md.",
    Flag.UNPUBLISHED_ARTIFACT: "Ran a local artifact instead of a pinned release, so it cannot be published{runs}.",
}


class Footnotes:
    """Numbered footnotes in order of first use. Equal texts share one number."""

    def __init__(self) -> None:
        self._texts: list[str] = []

    def __len__(self) -> int:
        return len(self._texts)

    def ref(self, text: str) -> str:
        """The markdown reference for ``text``, e.g. ``[^3]``."""
        if text not in self._texts:
            self._texts.append(text)
        return f"[^{self._texts.index(text) + 1}]"

    def refs(self, texts: Sequence[str]) -> str:
        return "".join(self.ref(t) for t in texts)

    def render(self) -> str:
        """Markdown footnote definitions, one per line."""
        return "\n".join(f"[^{i}]: {text}" for i, text in enumerate(self._texts, start=1))


def _runs(count: int, total: int) -> str:
    return "" if count >= total else f" (in {count} of {total} runs)"


def flag_text(flag: str, *, count: int, total: int, arm: Mapping[str, Any]) -> str:
    """Footnote for a run flag. ``flag`` may carry a parameter (``known_issue:<id>``)."""
    name, _, param = flag.partition(":")
    template = _FLAG_TEXT.get(name)
    runs = _runs(count, total)
    if template is None:
        detail = f" `{param}`" if param else ""
        return f"Flag `{name}`{detail}{runs}."
    return template.format(
        param=param or "?",
        runs=runs,
        server=arm.get("server") or "?",
        bds=arm.get("bds") or "?",
    )


def override_text(override: Mapping[str, Any]) -> str | None:
    """Footnote for a ``rescue`` or ``nonvanilla`` override; None for categories that are not footnoted."""
    category = override.get("category")
    if category not in (OverrideCategory.RESCUE, OverrideCategory.NONVANILLA):
        return None
    default = override.get("default")
    change = f"`{override.get('file')}` `{override.get('key')}` = `{override.get('value')}`"
    change += f" (default `{default}`)" if default is not None else " (not set by default)"
    if category == OverrideCategory.RESCUE:
        lead, tail = "Rescue override", "The default config does not survive this workload."
    else:
        lead, tail = "Non-vanilla override", "It changes gameplay to save work."
    reason = str(override.get("reason") or "").rstrip(".")
    return f"{lead}: {change}: {reason}. {tail}"


def status_text(runs: Sequence[Mapping[str, Any]]) -> str | None:
    """Footnote listing the runs that are not ok and why; None when every run is ok."""
    parts = []
    for status in (Status.SERVER_FAILED, Status.INVALID):
        failed = [r for r in runs if r.get("status") == status]
        if not failed:
            continue
        details = "; ".join(
            f"run {r.get('repeat')}: {', '.join(r.get('reasons') or ()) or 'no reason recorded'}" for r in failed
        )
        parts.append(f"{status.value} in {len(failed)} of {len(runs)} runs ({details})")
    if not parts:
        return None
    return "; ".join(parts) + ". Runs that are not ok are left out of the statistics."


def availability_text(label: str, agg: Mapping[str, Any], n_ok: int) -> str | None:
    """Footnote for a metric that some or all ok runs could not provide; None when every ok run measured it."""
    reasons = ", ".join(agg.get("reasons") or ())
    detail = f" ({reasons})" if reasons else ""
    if agg.get("availability") == "not_applicable":
        return f"{label}: not applicable{detail}."
    missing = n_ok - int(agg.get("n") or 0)
    if missing <= 0:
        return None
    return f"{label}: unavailable in {missing} of {n_ok} ok runs{detail}."


__all__ = ["Footnotes", "availability_text", "flag_text", "override_text", "status_text"]
