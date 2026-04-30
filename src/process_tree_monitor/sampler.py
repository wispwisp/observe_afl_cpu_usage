"""psutil-based CPU sampler for one or more process trees."""
from __future__ import annotations

import time
from typing import Iterable

import psutil

from .samples import CpuTimes, MemoryInfo, ProcSample, RootSample, Sample


class PsutilTreeSampler:
    """Walks the descendant tree(s) of one or more root PIDs and produces a
    Sample each tick using psutil.Process.cpu_times — cumulative seconds
    since process start (user, system, children_user, children_system).
    No per-PID Process cache is maintained: cpu_times() is not delta-based,
    so a fresh psutil.Process per tick is correct and simpler.
    """

    def __init__(self, root_pids: Iterable[int]) -> None:
        # Coerce to int, dedupe while preserving insertion order (so
        # per-root output keeps the order the caller passed, which
        # matters for AFL -M/-S where the main fuzzer is conventionally
        # first), and reject empty here rather than letting sample()
        # silently produce a useless aggregate later.
        deduped = list(dict.fromkeys(int(p) for p in root_pids))
        if not deduped:
            raise ValueError("root_pids must be non-empty")
        self._root_pids: list[int] = deduped

    def sample(self, *, interval_s: float, top_n: int) -> Sample:
        # Capture wall time once so every PID in this Sample shares one
        # timestamp regardless of how long the per-PID work below takes.
        wall_now = time.time()
        per_root_descendants, all_pids = _discover_tree(self._root_pids)
        proc_samples = _read_proc_samples(all_pids)
        roots = _aggregate_per_root(per_root_descendants, proc_samples)
        overall = _aggregate_overall(proc_samples)
        top = _select_top_processes(proc_samples, top_n)
        return Sample(
            timestamp_unix=wall_now,
            interval_s=interval_s,
            roots=roots,
            process_count=len(proc_samples),
            aggregate=overall,
            top_processes=top,
        )


def _discover_tree(
    root_pids: list[int],
) -> tuple[dict[int, list[int]], set[int]]:
    """Walk each root's descendant tree via psutil.

    A dead/gone/inaccessible root contributes an empty descendant
    list; its root_pid is still added to all_pids so
    _read_proc_samples will attempt it and _aggregate_per_root can
    later report root_alive=False. children(recursive=True) walks
    the tree at call time, so short-lived grandchildren born and
    reaped between ticks are missed here, but their CPU is still
    captured via their (still-alive) parent's children_*.
    """
    per_root_descendants: dict[int, list[int]] = {}
    all_pids: set[int] = set()
    for root_pid in root_pids:
        desc: list[int] = []
        try:
            proc = psutil.Process(root_pid)
            desc = [d.pid for d in proc.children(recursive=True)]
        except psutil.Error:
            pass
        per_root_descendants[root_pid] = desc
        all_pids.add(root_pid)
        all_pids.update(desc)
    return per_root_descendants, all_pids


def _read_proc_samples(pids: Iterable[int]) -> dict[int, ProcSample]:
    """Read cumulative CPU times, comm, and memory info for each PID.

    cpu_times() returns absolute cumulative seconds since process
    start; the first call on a fresh psutil.Process is correct
    without priming. memory_info() returns instantaneous RSS/VMS/
    shared (a gauge, not a counter) — there is no equivalent of
    cpu_times.children_*, so memory of a process that exits between
    ticks is gone. PIDs that vanish mid-read are silently dropped.
    comm is truncated to 15 chars to match /proc/<pid>/comm.
    """
    proc_samples: dict[int, ProcSample] = {}
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            t = proc.cpu_times()
            name = proc.name()
            mem = proc.memory_info()
        except psutil.Error:
            continue
        proc_samples[pid] = ProcSample(
            pid=pid,
            comm=name[:15],
            cpu_times=CpuTimes(
                user_seconds=t.user,
                system_seconds=t.system,
                children_user_seconds=t.children_user,
                children_system_seconds=t.children_system,
            ),
            memory=MemoryInfo(
                rss_bytes=mem.rss,
                vms_bytes=mem.vms,
                shared_bytes=mem.shared,
            ),
        )
    return proc_samples


def _aggregate_per_root(
    per_root_descendants: dict[int, list[int]],
    proc_samples: dict[int, ProcSample],
) -> tuple[RootSample, ...]:
    """Build one RootSample per root.

    For each root, sum CpuTimes over (root + descendants) PIDs that
    produced a sample. A zombie/already-reaped root yields
    root_alive=False but its surviving descendants still contribute
    to its aggregate, so the caller can see leftover work in the
    tree even after the root itself exits.
    """
    return tuple(
        RootSample(
            root_pid=root_pid,
            root_alive=root_pid in proc_samples,
            descendant_count=len(desc),
            aggregate=_sum_cpu_times(
                proc_samples[p].cpu_times
                for p in (root_pid, *desc)
                if p in proc_samples
            ),
        )
        for root_pid, desc in per_root_descendants.items()
    )


def _aggregate_overall(proc_samples: dict[int, ProcSample]) -> CpuTimes:
    """Sum CpuTimes across all sampled PIDs.

    Auto-deduped by pid via the dict keys: a descendant shared
    between two roots is counted once. This is the cross-tree
    aggregate, distinct from the per-root sub-aggregates produced
    by _aggregate_per_root.
    """
    return _sum_cpu_times(p.cpu_times for p in proc_samples.values())


def _select_top_processes(
    proc_samples: dict[int, ProcSample],
    top_n: int,
) -> tuple[ProcSample, ...]:
    """Return the top-N hottest ProcSamples by cumulative lifetime CPU.

    "Top by total cumulative" — the AFL master will usually
    dominate because it's been running longest. This is not "top
    by current rate".
    """
    return tuple(sorted(
        proc_samples.values(),
        key=lambda p: (
            p.cpu_times.user_seconds
            + p.cpu_times.system_seconds
            + p.cpu_times.children_user_seconds
            + p.cpu_times.children_system_seconds
        ),
        reverse=True,
    )[:top_n])


def _sum_cpu_times(items: Iterable[CpuTimes]) -> CpuTimes:
    u = s = cu = cs = 0.0
    for c in items:
        u += c.user_seconds
        s += c.system_seconds
        cu += c.children_user_seconds
        cs += c.children_system_seconds
    return CpuTimes(
        user_seconds=u,
        system_seconds=s,
        children_user_seconds=cu,
        children_system_seconds=cs,
    )
