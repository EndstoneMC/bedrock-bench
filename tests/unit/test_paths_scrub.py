# SPDX-License-Identifier: Apache-2.0
"""run.json and friends must not carry absolute paths, the user name or the host name."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bedrock_bench.provenance import Scrubber, scrub

WINDOWS = os.name == "nt"


def scrubber(**roots: str) -> Scrubber:
    return Scrubber({f"<{k}>": v for k, v in roots.items()}, user="benchuser", host="bench-host-01")


class TestRoots:
    @pytest.mark.skipif(not WINDOWS, reason="Windows path syntax")
    def test_windows_root_both_separators(self) -> None:
        s = scrubber(server="C:\\bb\\w\\abc123")
        assert s.text("C:\\bb\\w\\abc123\\crash_reports\\crash-1.txt") == "<server>/crash_reports/crash-1.txt"
        assert s.text("c:/bb/w/abc123/worlds/bench") == "<server>/worlds/bench"
        assert s.text("saved to: C:\\bb\\w\\abc123\\x.txt now") == "saved to: <server>/x.txt now"

    @pytest.mark.skipif(WINDOWS, reason="POSIX path syntax")
    def test_posix_root(self) -> None:
        s = scrubber(server="/srv/bb/w/abc123")
        assert s.text("/srv/bb/w/abc123/crash_reports/crash-1.txt") == "<server>/crash_reports/crash-1.txt"
        assert s.text("/srv/bb/w/abc1234/x") == "/srv/bb/w/abc1234/x"

    def test_most_specific_root_wins(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        work = cache / "w" / "abc"
        s = scrubber(cache=str(cache), server=str(work))
        assert s.text(str(work / "server.properties")) == "<server>/server.properties"
        assert s.text(str(cache / "dl" / "x.zip")) == "<cache>/dl/x.zip"

    def test_root_ends_at_a_separator(self, tmp_path: Path) -> None:
        s = scrubber(run=str(tmp_path / "abc"))
        assert not s.text(str(tmp_path / "abc1" / "x")).startswith("<run>")
        assert s.text(str(tmp_path / "abc" / "x")) == "<run>/x"

    def test_root_itself(self, tmp_path: Path) -> None:
        s = scrubber(run=str(tmp_path / "run"))
        assert s.text(str(tmp_path / "run")) == "<run>"

    def test_default_roots(self) -> None:
        home = Path.home() / "projects" / "thing.txt"
        assert Scrubber().text(str(home)).endswith("/projects/thing.txt")
        assert not Scrubber().text(str(home)).startswith(str(Path.home()))


class TestLeftovers:
    def test_unknown_windows_path(self) -> None:
        s = scrubber()
        assert s.text("D:\\Somewhere\\Else\\bedrock_server.exe") == "<abs>/bedrock_server.exe"
        assert s.text("see \\\\nas\\share\\file.log") == "see <abs>/file.log"

    def test_private_posix_path(self) -> None:
        s = scrubber()
        assert s.text("/home/someone/bench/run.log") == "<abs>/run.log"
        assert s.text("/mnt/data/xyz") == "<abs>/xyz"

    def test_system_paths_and_urls_kept(self) -> None:
        s = scrubber()
        assert s.text("/lib/x86_64-linux-gnu/libc.so.6") == "/lib/x86_64-linux-gnu/libc.so.6"
        url = "https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-1.26.52.3.zip"
        assert s.text(url) == url
        assert s.text("files.pythonhosted.org/packages/home/x") == "files.pythonhosted.org/packages/home/x"

    def test_placeholder_paths_are_not_scrubbed_again(self) -> None:
        s = scrubber()
        assert s.text("<server>/tmp/file") == "<server>/tmp/file"

    def test_user_and_host_words(self) -> None:
        s = scrubber()
        assert s.text("started by benchuser on bench-host-01") == "started by <user> on <host>"
        assert s.text("BENCHUSER") == "<user>"
        assert s.text("benchusers bench-host-012") == "benchusers bench-host-012"

    def test_short_or_common_names_are_not_words(self) -> None:
        s = Scrubber(user="root", host="ab")
        assert s.text("root cause in ab") == "root cause in ab"


class TestStructures:
    def test_nested_data_and_keys(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "m" / "r1"
        s = scrubber(run=str(run_dir))
        data = {
            "files": {str(run_dir / "server.log.gz"): {"bytes": 10}},
            "argv": [str(run_dir / "bots.exe"), "-addr", "127.0.0.1:19672"],
            "tuple": (str(run_dir), 1, None, True, 2.5),
            "note": "made by benchuser",
        }
        out = s(data)
        assert out["files"] == {"<run>/server.log.gz": {"bytes": 10}}
        assert out["argv"] == ["<run>/bots.exe", "-addr", "127.0.0.1:19672"]
        assert out["tuple"] == ["<run>", 1, None, True, 2.5]
        assert out["note"] == "made by <user>"
        assert s.leaks(out) == []
        json.dumps(out)

    def test_path_objects(self, tmp_path: Path) -> None:
        s = scrubber(run=str(tmp_path))
        assert s([tmp_path / "a.txt"]) == ["<run>/a.txt"]

    def test_leaks_detects_what_is_left(self) -> None:
        s = scrubber()
        leaky = {"a": "C:\\Users\\x\\file", "b": ["/home/x/y"], "c": "bench-host-01", "ok": "fine"}
        assert sorted(s.leaks(leaky)) == sorted(["C:\\Users\\x\\file", "/home/x/y", "bench-host-01"])
        assert s.leaks(s(leaky)) == []

    def test_run_json_like_document(self, tmp_path: Path) -> None:
        work = tmp_path / "w" / "abc"
        doc = {
            "schema": "bedrock-bench/run/1",
            "server": {"facts": {"allocator": "default"}},
            "health": {"crash_files": [str(work / "crash_reports" / "crash-2026.txt")]},
            "first_errors": [f"This crash report has been saved to: {work / 'crash_reports' / 'crash-2026.txt'}"],
            "artifacts": [{"name": "bedrock-server-1.26.52.3.zip", "sha256": "0" * 64}],
        }
        out = scrub(doc, {"<server>": work})
        assert out["health"]["crash_files"] == ["<server>/crash_reports/crash-2026.txt"]
        assert out["first_errors"] == ["This crash report has been saved to: <server>/crash_reports/crash-2026.txt"]
        assert out["artifacts"] == doc["artifacts"]
        assert Scrubber({"<server>": work}).leaks(out) == []
