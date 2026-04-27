"""psutil-based CPU sampler for one or more process trees."""
from __future__ import annotations

import logging
import os
import time
from typing import Iterable

import psutil

from .samples import ProcSample, RootSample, SCHEMA_VERSION, Sample

log = logging.getLogger(__name__)


def _num_cpus() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


class PsutilTreeSampler:
    """Walks the descendant tree(s) of one or more root PIDs and produces a
    Sample each tick using psutil.Process.cpu_percent (delta-based,
    non-blocking).

    A per-PID cache of psutil.Process is kept across ticks because
    cpu_percent(interval=None) is computed against the previous call on
    the same instance. The cache is keyed by pid with create_time()
    verified on each hit so PID reuse cannot poison a reading.

    First-sample caveat: psutil.Process.cpu_percent(interval=None)
    returns 0.0 on its priming call. attach() primes only the root
    PIDs, so on the first sample() any descendant encountered for the
    first time is being primed and reads 0%. Aggregations that span
    the very first tick will therefore underreport CPU. From the
    second tick onward, every PID seen for at least one full tick
    interval reports a real delta.
    """

    def __init__(self, root_pids: Iterable[int]) -> None:
        deduped = list(dict.fromkeys(int(p) for p in root_pids))
        if not deduped:
            raise ValueError("root_pids must be non-empty")
        self._root_pids: list[int] = deduped
        self._cache: dict[int, tuple[psutil.Process, float]] = {}
        self._last_monotonic: float | None = None
        self._attached = False
        self._ncpu = _num_cpus()

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
        self._last_monotonic = None
        self._attached = False

    def _get_or_prime(self, pid: int) -> psutil.Process | None:
        cached = self._cache.get(pid)
        if cached is not None:
            proc, ct = cached
            try:
                if proc.create_time() == ct:
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            self._cache.pop(pid, None)
        try:
            proc = psutil.Process(pid)
            ct = proc.create_time()
            proc.cpu_percent(interval=None)
            self._cache[pid] = (proc, ct)
            return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None

    @staticmethod
    def _is_alive(proc: psutil.Process) -> bool:
        try:
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def sample(self, *, interval_s: float, top_n: int) -> Sample:
        if not self._attached:
            raise RuntimeError("attach() must be called before sample()")
        t0 = time.monotonic()
        wall_now = time.time()
        elapsed = t0 - (self._last_monotonic if self._last_monotonic is not None else t0)
        self._last_monotonic = t0
        ncpu = self._ncpu

        per_root_descendants: dict[int, list[int]] = {}
        live_root_pids: set[int] = set()
        all_pids: set[int] = set()

        for root_pid in self._root_pids:
            root_proc = self._get_or_prime(root_pid)
            if root_proc is None or not self._is_alive(root_proc):
                per_root_descendants[root_pid] = []
                continue
            try:
                desc_pids = [d.pid for d in root_proc.children(recursive=True)]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                per_root_descendants[root_pid] = []
                continue
            live_root_pids.add(root_pid)
            per_root_descendants[root_pid] = desc_pids
            all_pids.add(root_pid)
            all_pids.update(desc_pids)

        for pid in list(self._cache.keys()):
            if pid not in all_pids and pid not in live_root_pids:
                self._cache.pop(pid, None)

        proc_samples: dict[int, ProcSample] = {}
        cpu_by_pid: dict[int, float] = {}
        dropped = 0
        for pid in all_pids:
            proc = self._get_or_prime(pid)
            if proc is None:
                dropped += 1
                continue
            try:
                with proc.oneshot():
                    cpu_pct = proc.cpu_percent(interval=None)
                    times = proc.cpu_times()
                    name = proc.name()
                    try:
                        ppid = proc.ppid()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        ppid = 0
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
        for root_pid in self._root_pids:
            if root_pid in live_root_pids:
                desc_pids = per_root_descendants[root_pid]
                pids = {root_pid, *desc_pids}
                agg = sum(cpu_by_pid.get(p, 0.0) for p in pids)
                roots.append(RootSample(
                    root_pid=root_pid,
                    root_alive=True,
                    descendant_count=len(desc_pids),
                    aggregate_cpu_percent=agg,
                ))
            else:
                roots.append(RootSample(
                    root_pid=root_pid,
                    root_alive=False,
                    descendant_count=0,
                    aggregate_cpu_percent=0.0,
                ))

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
