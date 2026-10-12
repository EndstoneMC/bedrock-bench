# SPDX-License-Identifier: Apache-2.0
"""Process counters of the server once a second, into ``series/process.csv.gz``.

User and system CPU come from the process counters, which include threads that have exited; this is the
cross-server CPU metric (``process_cores``). Memory is the working set and private bytes on Windows, ``VmRSS`` and
``RssAnon + VmSwap`` on Linux. When a launcher started the server (the Endstone CLI), the ``tree_*`` columns hold the
CPU the launcher's tree, without the server, has used since sampling began (``aux_cores``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psutil

from bedrock_bench import platform
from bedrock_bench.clock import mono_ns
from bedrock_bench.platform import ProcessGone
from bedrock_bench.sampling.base import PeriodicSampler, ProcessGroup, children_map, descendants
from bedrock_bench.sampling.series import PROCESS_COLUMNS, SeriesWriter, fmt


class ProcessSampler(PeriodicSampler):
    def __init__(
        self,
        server: psutil.Process,
        path: Path,
        *,
        launcher: psutil.Process | None = None,
        interval_s: float = 1.0,
        tree_refresh_s: float = 5.0,
        clock: Callable[[], int] = mono_ns,
    ) -> None:
        super().__init__(f"bbench-process-{server.pid}", interval_s, clock=clock)
        self.server = server
        self.path = Path(path)
        self._impl = platform.current()
        self._tree = ProcessGroup([launcher]) if launcher is not None and launcher.pid != server.pid else None
        self._refresh_every = max(1, round(tree_refresh_s / interval_s))
        self._writer: SeriesWriter | None = None

    def setup(self) -> None:
        self._writer = SeriesWriter(self.path, PROCESS_COLUMNS)
        self._refresh_tree()

    def sample(self) -> None:
        pid = self.server.pid
        try:
            with self.server.oneshot():
                times = self.server.cpu_times()
                threads = self.server.num_threads()
        except psutil.NoSuchProcess:
            raise ProcessGone(pid) from None
        t_ns = self.clock()
        resident, private = self._impl.process_memory(pid)
        handles = self._impl.process_handles(pid)
        tree_user = tree_sys = None
        if self._tree is not None:
            if self.samples % self._refresh_every == 0:
                self._refresh_tree()
            tree_user, tree_sys = self._tree.cpu()
        assert self._writer is not None
        self._writer.write(
            [
                t_ns,
                fmt(times.user),
                fmt(times.system),
                fmt(resident, 3),
                fmt(private, 3),
                threads,
                fmt(handles),
                fmt(tree_user),
                fmt(tree_sys),
            ]
        )

    def teardown(self) -> None:
        if self._writer is not None:
            self._writer.close()

    def _refresh_tree(self) -> None:
        if self._tree is None:
            return
        children = children_map()
        self._tree.refresh(children, exclude=descendants(children, self.server.pid) | {self.server.pid})
