"""psutil-based CPU sampler for one or more process trees."""
from __future__ import annotations

import time
from typing import Iterable

import psutil

from .samples import ProcSample, RootSample, Sample


class PsutilTreeSampler:
    """Walks the descendant tree(s) of one or more root PIDs and produces a
    Sample each tick using psutil.Process.cpu_percent (delta-based,
    non-blocking). A per-PID psutil.Process cache is kept across ticks
    because cpu_percent(interval=None) is computed against the previous
    call on the same instance.
    """

    def __init__(self, root_pids: Iterable[int]) -> None:
        deduped = list(dict.fromkeys(int(p) for p in root_pids))
        if not deduped:
            raise ValueError("root_pids must be non-empty")
        self._root_pids: list[int] = deduped
        self._cache: dict[int, psutil.Process] = {}
        self._ncpu = psutil.cpu_count() or 1
        for pid in self._root_pids:
            self._prime(pid)

    def _prime(self, pid: int) -> psutil.Process | None:
        proc = self._cache.get(pid)
        if proc is not None:
            return proc
        try:
            proc = psutil.Process(pid)
            proc.cpu_percent(interval=None)
        except psutil.Error:
            return None
        self._cache[pid] = proc
        return proc

    def sample(self, *, interval_s: float, top_n: int) -> Sample:
        wall_now = time.time()

        per_root_descendants: dict[int, list[int]] = {}
        all_pids: set[int] = set()
        for root_pid in self._root_pids:
            proc = self._prime(root_pid)
            if proc is None or not proc.is_running():
                per_root_descendants[root_pid] = []
                continue
            try:
                desc = [d.pid for d in proc.children(recursive=True)]
            except psutil.Error:
                desc = []
            per_root_descendants[root_pid] = desc
            all_pids.add(root_pid)
            all_pids.update(desc)

        proc_samples: dict[int, ProcSample] = {}
        for pid in all_pids:
            proc = self._prime(pid)
            if proc is None:
                continue
            try:
                cpu = float(proc.cpu_percent(interval=None))
                name = proc.name()
            except psutil.Error:
                continue
            proc_samples[pid] = ProcSample(pid=pid, comm=name[:15], cpu_percent=cpu)

        for pid in list(self._cache):
            if pid not in all_pids:
                del self._cache[pid]

        roots: list[RootSample] = []
        for root_pid, desc in per_root_descendants.items():
            agg = sum(
                proc_samples[p].cpu_percent
                for p in (root_pid, *desc)
                if p in proc_samples
            )
            roots.append(RootSample(
                root_pid=root_pid,
                root_alive=root_pid in proc_samples,
                descendant_count=len(desc),
                aggregate_cpu_percent=agg,
            ))

        aggregate = sum(p.cpu_percent for p in proc_samples.values())
        top = tuple(
            sorted(proc_samples.values(), key=lambda p: p.cpu_percent, reverse=True)[:top_n]
        )
        return Sample(
            timestamp_unix=wall_now,
            interval_s=interval_s,
            ncpu=self._ncpu,
            roots=tuple(roots),
            process_count=len(proc_samples),
            aggregate_cpu_percent=aggregate,
            normalized_cpu_percent=aggregate / self._ncpu,
            top_processes=top,
        )
