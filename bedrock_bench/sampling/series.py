# SPDX-License-Identifier: Apache-2.0
"""Series files: the CSV formats the samplers write under ``<run>/series/`` and the readers that load them.

Every file starts with a header row; read columns by name. ``.gz`` files are gzip with a zero timestamp, so equal
content gives equal bytes. Writers flush every few seconds, and the readers accept a file cut off by a crash: a
truncated gzip stream or a final line without its newline is dropped, everything before it is kept.

All ``t_ns``, ``send_ns`` and ``rx_ns`` values are harness monotonic nanoseconds (``bedrock_bench.clock.mono_ns``).

=========================  ==========================================================================================
File                       Columns
=========================  ==========================================================================================
``probe.csv``              ``send_ns,rx_ns,tick``
``threads.csv.gz``         ``t_ns,tid,name,cpu_ns[,cycles]`` (one row per thread per sample; exited threads keep
                           their rows, and Windows writes a final row when a thread exits)
``tick.csv.gz``            ``t_ns,cpu_ns[,cycles]``
``process.csv.gz``         ``t_ns,user_s,sys_s,resident_mib,private_mib,threads,handles,tree_user_s,tree_sys_s``
``machine.csv.gz``         ``t_ns,foreign_cores,foreign_server_set_cores,server_cores,bot_cores,harness_cores,``
                           ``cpu0..cpuN`` (busy fraction per logical CPU)
``foreign_top.jsonl``      ``{"t_ns", "interval_s", "top": [{"pid", "name", "cores"}]}`` every 10 s
=========================  ==========================================================================================

``cycles`` (TSC ticks) is written for the tick thread on Windows; readers accept it in ``threads.csv.gz`` too.
``handles`` is empty off Windows. The ``tree_*`` columns are empty when the server was started directly rather than
by a launcher. The ``*_cores`` group columns of ``machine.csv.gz`` are the CPU of the harness's own process trees.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import time
import zlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from bedrock_bench.model import CpuSample, MachineSample, ProbeSample, ProcSample, ThreadSample, ThreadSeries

PROBE_FILE = "probe.csv"
THREADS_FILE = "threads.csv.gz"
TICK_FILE = "tick.csv.gz"
PROCESS_FILE = "process.csv.gz"
MACHINE_FILE = "machine.csv.gz"
FOREIGN_TOP_FILE = "foreign_top.jsonl"

PROBE_COLUMNS = ("send_ns", "rx_ns", "tick")
THREAD_COLUMNS = ("t_ns", "tid", "name", "cpu_ns")
TICK_COLUMNS = ("t_ns", "cpu_ns")
PROCESS_COLUMNS = (
    "t_ns",
    "user_s",
    "sys_s",
    "resident_mib",
    "private_mib",
    "threads",
    "handles",
    "tree_user_s",
    "tree_sys_s",
)
MACHINE_GROUPS = ("server", "bot", "harness")
MACHINE_COLUMNS = ("t_ns", "foreign_cores", "foreign_server_set_cores", *(f"{g}_cores" for g in MACHINE_GROUPS))
CYCLES_COLUMN = "cycles"


def fmt(value: float | int | None, digits: int = 6) -> str:
    """A compact, deterministic number for CSV: ints as is, floats to ``digits`` decimals without trailing zeros."""
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


class SeriesWriter:
    """Writes one series file. ``.gz`` paths are compressed; rows are flushed at least every ``flush_s`` seconds."""

    def __init__(self, path: Path, columns: Sequence[str], *, flush_s: float = 5.0) -> None:
        self.path = Path(path)
        self.columns = tuple(columns)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._raw = open(self.path, "wb")
        self._gz = (
            gzip.GzipFile(filename="", mode="wb", fileobj=self._raw, compresslevel=6, mtime=0)
            if self.path.suffix == ".gz"
            else None
        )
        self._text = io.TextIOWrapper(self._gz or self._raw, encoding="utf-8", newline="", write_through=False)
        self._csv = csv.writer(self._text, lineterminator="\n")
        self._csv.writerow(self.columns)
        self._flush_s = flush_s
        self._last_flush = time.monotonic()
        self.rows = 0

    def write(self, row: Sequence[Any]) -> None:
        if len(row) != len(self.columns):
            raise ValueError(f"{self.path.name}: row has {len(row)} fields, header has {len(self.columns)}")
        self._csv.writerow(row)
        self.rows += 1
        if time.monotonic() - self._last_flush >= self._flush_s:
            self.flush()

    def flush(self) -> None:
        self._text.flush()
        if self._gz is not None:
            self._gz.flush()
        self._raw.flush()
        self._last_flush = time.monotonic()

    def close(self) -> None:
        if self._raw.closed:
            return
        self._text.close()  # closes the gzip stream and writes its trailer
        self._raw.close()

    def __enter__(self) -> SeriesWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class JsonlWriter:
    """Append-only JSON lines, flushed per line."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w", encoding="utf-8", newline="\n")

    def write(self, obj: Any) -> None:
        self._fh.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------------------------------------------------


def iter_lines(path: Path, chunk: int = 1 << 20) -> Iterator[str]:
    """Complete lines of a text or ``.gz`` file; a cut-off tail is dropped."""
    path = Path(path)
    with open(path, "rb") as raw:
        if path.suffix != ".gz":
            data = raw.read()
            lines = data.split(b"\n")
            for line in lines[:-1]:
                yield line.decode("utf-8", errors="replace")
            return
        decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
        pending = b""
        while not decoder.eof:
            block = raw.read(chunk)
            if not block:
                break
            try:
                data = pending + decoder.decompress(block)
            except zlib.error as exc:
                raise ValueError(f"{path}: corrupt gzip data: {exc}") from exc
            *complete, pending = data.split(b"\n")
            for line in complete:
                yield line.decode("utf-8", errors="replace")
        if decoder.eof and pending:
            yield pending.decode("utf-8", errors="replace")


def iter_rows(path: Path) -> Iterator[dict[str, str]]:
    """Rows of a series CSV as ``{column: text}``."""
    reader = csv.reader(iter_lines(path))
    header = next(reader, None)
    if header is None:
        return
    for row in reader:
        if len(row) == len(header):
            yield dict(zip(header, row, strict=True))


def _int(text: str) -> int | None:
    return int(text) if text else None


def _float(text: str) -> float | None:
    return float(text) if text else None


def read_probe(path: Path) -> list[ProbeSample]:
    return [ProbeSample(int(r["send_ns"]), int(r["rx_ns"]), int(r["tick"])) for r in iter_rows(path)]


def read_threads(path: Path) -> ThreadSeries:
    return ThreadSeries(
        [
            ThreadSample(int(r["t_ns"]), int(r["tid"]), r["name"], int(r["cpu_ns"]), _int(r.get(CYCLES_COLUMN, "")))
            for r in iter_rows(path)
        ]
    )


def read_tick(path: Path) -> list[CpuSample]:
    return [CpuSample(int(r["t_ns"]), int(r["cpu_ns"]), _int(r.get(CYCLES_COLUMN, ""))) for r in iter_rows(path)]


def read_process(path: Path) -> list[ProcSample]:
    out = []
    for r in iter_rows(path):
        tree_user, tree_sys = _float(r["tree_user_s"]), _float(r["tree_sys_s"])
        out.append(
            ProcSample(
                t_ns=int(r["t_ns"]),
                cpu_user_s=float(r["user_s"]),
                cpu_sys_s=float(r["sys_s"]),
                resident_mib=float(r["resident_mib"]),
                private_mib=float(r["private_mib"]),
                threads=int(r["threads"]),
                handles=_int(r["handles"]),
                tree_cpu_s=None if tree_user is None or tree_sys is None else tree_user + tree_sys,
            )
        )
    return out


def _cpu_columns(row: dict[str, str]) -> list[str]:
    return sorted((k for k in row if k.startswith("cpu") and k[3:].isdigit()), key=lambda k: int(k[3:]))


def read_machine(path: Path) -> list[MachineSample]:
    out = []
    columns: list[str] | None = None
    for r in iter_rows(path):
        columns = columns or _cpu_columns(r)
        out.append(
            MachineSample(
                t_ns=int(r["t_ns"]),
                per_cpu_busy=tuple(float(r[c]) for c in columns),
                foreign_cores=float(r["foreign_cores"]),
                foreign_server_set_cores=float(r["foreign_server_set_cores"]),
            )
        )
    return out


def read_group_cores(path: Path) -> list[tuple[int, dict[str, float]]]:
    """``(t_ns, {"server": cores, "bot": cores, "harness": cores})`` per machine sample."""
    return [(int(r["t_ns"]), {g: float(r[f"{g}_cores"]) for g in MACHINE_GROUPS}) for r in iter_rows(path)]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    for line in iter_lines(path):
        if line.strip():
            out.append(json.loads(line))
    return out
