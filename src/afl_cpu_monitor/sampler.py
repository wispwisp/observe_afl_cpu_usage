"""Tree-walking CPU sampler built on psutil + /proc."""
from __future__ import annotations

import logging
import time
from typing import Iterable

import psutil

from .proc import (
    build_children_map,
    descendants,
    num_cpus,
    read_pid_ppid_map,
)
from .samples import ProcSample, RootSample, SCHEMA_VERSION, Sample

log = logging.getLogger(__name__)


class PsutilTreeSampler:
    """Walks the descendant tree(s) of one or more root PIDs and produces a
    Sample each tick using psutil.Process.cpu_percent (delta-based,
    non-blocking).

    A per-PID cache of psutil.Process is kept across ticks because
    cpu_percent(interval=None) is computed against the previous call on
    the same instance. New PIDs are primed on the tick they're first seen
    and contribute 0 % on that tick (psutil semantics).
    """

    def __init__(self, root_pids: Iterable[int]) -> None:
        deduped = list(dict.fromkeys(int(p) for p in root_pids))
        if not deduped:
            raise ValueError("root_pids must be non-empty")
        self._root_pids: list[int] = deduped
        self._cache: dict[int, psutil.Process] = {}
        self._create_times: dict[int, float] = {}
        self._last_monotonic: float | None = None
        self._attached = False

    @property
    def root_pids(self) -> list[int]:
        return list(self._root_pids)

    def attach(self) -> None:
        for pid in self._root_pids:
            self._get_or_prime(pid)
        self._last_monotonic = time.monotonic()
        self._attached = True

    def detach(self) -> None:
        self._cache.clear()
        self._create_times.clear()
        self._last_monotonic = None
        self._attached = False

    def _get_or_prime(self, pid: int) -> psutil.Process | None:
        cached = self._cache.get(pid)
        if cached is not None:
            try:
                if cached.create_time() == self._create_times.get(pid):
                    return cached
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            self._cache.pop(pid, None)
            self._create_times.pop(pid, None)
        try:
            proc = psutil.Process(pid)
            ct = proc.create_time()
            proc.cpu_percent(interval=None)
            self._cache[pid] = proc
            self._create_times[pid] = ct
            return proc
        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
            ProcessLookupError,
        ):
            return None

    def sample(self, *, interval_s: float, top_n: int) -> Sample:
        if not self._attached:
            raise RuntimeError("attach() must be called before sample()")
        t0 = time.monotonic()
        wall_now = time.time()
        elapsed = t0 - (self._last_monotonic if self._last_monotonic is not None else t0)
        self._last_monotonic = t0

        ppid_map = read_pid_ppid_map()
        children = build_children_map(ppid_map)
        ncpu = num_cpus()

        per_root_pids: dict[int, set[int]] = {}
        all_pids: set[int] = set()
        for root in self._root_pids:
            root_alive = root in ppid_map
            desc = descendants(root, children) if root_alive else set()
            per_root_pids[root] = desc
            if root_alive:
                all_pids.add(root)
                all_pids.update(desc)

        for pid in list(self._cache.keys()):
            if pid not in all_pids and pid not in self._root_pids:
                self._cache.pop(pid, None)
                self._create_times.pop(pid, None)

        proc_samples: dict[int, ProcSample] = {}
        cpu_by_pid: dict[int, float] = {}
        dropped = 0
        for pid in all_pids:
            try:
                proc = self._get_or_prime(pid)
                if proc is None:
                    dropped += 1
                    continue
                with proc.oneshot():
                    cpu_pct = proc.cpu_percent(interval=None)
                    times = proc.cpu_times()
                    name = proc.name()
                    try:
                        ppid = proc.ppid()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        ppid = ppid_map.get(pid, 0)
                cpu_by_pid[pid] = float(cpu_pct)
                proc_samples[pid] = ProcSample(
                    pid=pid,
                    ppid=ppid,
                    comm=name[:15],
                    cpu_percent=float(cpu_pct),
                    cpu_time_user_s=float(times.user),
                    cpu_time_system_s=float(times.system),
                )
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
                ProcessLookupError,
            ):
                dropped += 1

        roots: list[RootSample] = []
        for root in self._root_pids:
            root_alive = root in ppid_map
            desc = per_root_pids[root]
            if root_alive:
                pids = desc | {root}
                agg = sum(cpu_by_pid.get(p, 0.0) for p in pids)
            else:
                agg = 0.0
            roots.append(
                RootSample(
                    root_pid=root,
                    root_alive=root_alive,
                    descendant_count=len(desc),
                    aggregate_cpu_percent=agg,
                )
            )

        aggregate = sum(cpu_by_pid.values())
        normalized = aggregate / max(1, ncpu)
        top = tuple(
            sorted(proc_samples.values(), key=lambda p: p.cpu_percent, reverse=True)[:top_n]
        )

        return Sample(
            schema_version=SCHEMA_VERSION,
            timestamp_unix=wall_now,
            monotonic_s=t0,
            interval_s=interval_s,
            elapsed_s=elapsed,
            ncpu=ncpu,
            roots=tuple(roots),
            process_count=len(proc_samples),
            aggregate_cpu_percent=aggregate,
            normalized_cpu_percent=normalized,
            top_processes=top,
            sampling_overhead_ms=(time.monotonic() - t0) * 1000.0,
            dropped_pids=dropped,
            notes=(),
        )
