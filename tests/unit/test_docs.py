# SPDX-License-Identifier: Apache-2.0
"""The tracked markdown: relative links resolve, the document set is fixed, and nothing cites the private spec."""

from __future__ import annotations

import posixpath
import re
import shutil
import subprocess
from functools import cache
from urllib.parse import unquote

import pytest

from bedrock_bench import adapters
from bedrock_bench.config import project_root, server_dir

ROOT = project_root()

if shutil.which("git") is None or not (ROOT / ".git").exists():
    pytest.skip("needs a git checkout", allow_module_level=True)

#: Markdown files allowed outside tests/, besides docs/blog/*.md and one README per server folder.
FIXED_DOCS = frozenset(
    {
        "README.md",
        ".github/CONTRIBUTING.md",
        "bots/README.md",
        "docs/get_started.md",
        "docs/methodology.md",
        "docs/metrics.md",
    }
)
BLOG = "docs/blog/"
PRIVATE_SPEC = b"SPEC" + b".md"

LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?", re.MULTILINE)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
HTML_ANCHOR = re.compile(r"<a\s+(?:id|name)=\"([^\"]+)\"", re.IGNORECASE)
EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


@cache
def tracked() -> frozenset[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return frozenset(path for path in out.decode("utf-8").split("\0") if path and (ROOT / path).exists())


def markdown() -> list[str]:
    return sorted(path for path in tracked() if path.lower().endswith(".md"))


def strip_code(text: str) -> str:
    return INLINE_CODE.sub("", FENCE.sub("", text))


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).replace("`", "").strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


@cache
def anchors(path: str) -> frozenset[str]:
    text = (ROOT / path).read_text(encoding="utf-8")
    found: set[str] = set(HTML_ANCHOR.findall(text))
    seen: dict[str, int] = {}
    for heading in HEADING.findall(FENCE.sub("", text)):
        base = slug(heading)
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return frozenset(found)


def broken_links(path: str) -> list[str]:
    text = strip_code((ROOT / path).read_text(encoding="utf-8"))
    files = tracked()
    problems = []
    for target in [*LINK.findall(text), *REFERENCE.findall(text)]:
        if not target or EXTERNAL.match(target):
            continue
        location, _, fragment = target.partition("#")
        location = unquote(location.split("?", 1)[0])
        if location.startswith("/"):
            resolved = posixpath.normpath(location.lstrip("/"))
        elif location:
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), location))
        else:
            resolved = path
        if resolved == "." or resolved.startswith("../"):
            problems.append(f"{target}: outside the repository")
        elif resolved not in files and not any(f.startswith(resolved + "/") for f in files):
            problems.append(f"{target}: no tracked file {resolved}")
        elif fragment and resolved.lower().endswith(".md") and unquote(fragment) not in anchors(resolved):
            problems.append(f"{target}: no heading #{fragment} in {resolved}")
    return problems


@pytest.mark.parametrize("path", markdown())
def test_relative_links_resolve(path: str) -> None:
    assert broken_links(path) == []


def test_markdown_set() -> None:
    built_in = [name for name, cls in adapters.all_adapters().items() if cls.__module__.startswith(adapters.__name__)]
    allowed = FIXED_DOCS | {(server_dir(name) / "README.md").relative_to(ROOT).as_posix() for name in built_in}
    docs = {path for path in markdown() if not path.startswith("tests/")}
    blog = {path for path in docs if path.startswith(BLOG) and "/" not in path[len(BLOG) :]}
    assert f"{BLOG}README.md" in blog
    reports = {path for path in docs if path.startswith("results/") and path.endswith("/report.md")}
    assert sorted(docs - blog - reports - allowed) == [], "markdown outside the allowed set"
    assert sorted(allowed - docs) == [], "missing markdown"


def test_nothing_cites_the_private_spec() -> None:
    assert sorted(path for path in tracked() if PRIVATE_SPEC in (ROOT / path).read_bytes()) == []


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        ("Supported servers", "supported-servers"),
        ("`-loadscreen`", "-loadscreen"),
        ("Protocol 1001", "protocol-1001"),
        ("Tick-thread CPU is not MSPT", "tick-thread-cpu-is-not-mspt"),
        ("Why trust the numbers?", "why-trust-the-numbers"),
        ("Fairness and feature parity", "fairness-and-feature-parity"),
    ],
)
def test_slug(heading: str, anchor: str) -> None:
    assert slug(heading) == anchor
