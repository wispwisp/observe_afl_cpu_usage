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
        wall_now = time.time()
        per_root_descendants, all_pids = _discover_tree(self._root_pids)
        proc_samples = _read_proc_samples(all_pids)
        roots = _aggregate_per_root(per_root_descendants, proc_samples, full_memory_info=False)
        cpu_overall, mem_overall, full_info_count = _aggregate_overall(
            proc_samples, full_memory_info=False,
        )
        top_cpu = _select_top_processes_by_cpu(proc_samples, top_n)
        top_mem = _select_top_processes_by_memory(proc_samples, top_n, full_memory_info=False)
        return Sample(
            timestamp_unix=wall_now,
            interval_s=interval_s,
            roots=roots,
            process_count=len(proc_samples),
            aggregate=cpu_overall,
            memory_aggregate=mem_overall,
            memory_full_info_pid_count=full_info_count,
            top_processes_by_cpu=top_cpu,
            top_processes_by_memory=top_mem,
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
    full_memory_info: bool,
) -> tuple[RootSample, ...]:
    """Build one RootSample per root.

    For each root, sum CpuTimes and MemoryInfo over (root +
    descendants) PIDs that produced a sample. A zombie/already-reaped
    root yields root_alive=False but its surviving descendants still
    contribute to its aggregates. memory_full_info_pid_count is None
    when full_memory_info is off; otherwise it counts root+descendant
    PIDs whose memory.pss_bytes is not None.
    """
    out: list[RootSample] = []
    for root_pid, desc in per_root_descendants.items():
        contributing = [
            proc_samples[p] for p in (root_pid, *desc) if p in proc_samples
        ]
        cpu_agg = _sum_cpu_times(p.cpu_times for p in contributing)
        mem_agg = _sum_memory_info(p.memory for p in contributing)
        full_count: int | None
        if full_memory_info:
            full_count = sum(1 for p in contributing if p.memory.pss_bytes is not None)
        else:
            full_count = None
        out.append(RootSample(
            root_pid=root_pid,
            root_alive=root_pid in proc_samples,
            descendant_count=len(desc),
            aggregate=cpu_agg,
            memory_aggregate=mem_agg,
            memory_full_info_pid_count=full_count,
        ))
    return tuple(out)


def _aggregate_overall(
    proc_samples: dict[int, ProcSample],
    full_memory_info: bool,
) -> tuple[CpuTimes, MemoryInfo, int | None]:
    """Sum CpuTimes and MemoryInfo across all sampled PIDs.

    Auto-deduped by pid via the dict keys: a descendant shared
    between two roots is counted once. Returns (cpu_agg, mem_agg,
    full_info_pid_count). full_info_pid_count is None when
    full_memory_info is off; otherwise it counts PIDs whose
    memory.pss_bytes is not None.
    """
    cpu_agg = _sum_cpu_times(p.cpu_times for p in proc_samples.values())
    mem_agg = _sum_memory_info(p.memory for p in proc_samples.values())
    full_count: int | None
    if full_memory_info:
        full_count = sum(
            1 for p in proc_samples.values() if p.memory.pss_bytes is not None
        )
    else:
        full_count = None
    return cpu_agg, mem_agg, full_count


def _select_top_processes_by_cpu(
    proc_samples: dict[int, ProcSample],
    top_n: int,
) -> tuple[ProcSample, ...]:
    """Return the top-N hottest ProcSamples by cumulative lifetime CPU.

    "Top by total cumulative" — the AFL master will usually
    dominate because it's been running longest. Not "top by current rate".
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


def _select_top_processes_by_memory(
    proc_samples: dict[int, ProcSample],
    top_n: int,
    full_memory_info: bool,
) -> tuple[ProcSample, ...]:
    """Return the top-N memory-heaviest ProcSamples.

    Ranks by pss_bytes when full_memory_info=True (treating None as 0
    so AccessDenied-fallback PIDs don't masquerade as zero-cost), else
    by rss_bytes. Snapshot ranking — memory is a gauge, not cumulative.
    """
    if full_memory_info:
        def key(p: ProcSample) -> int:
            return p.memory.pss_bytes if p.memory.pss_bytes is not None else 0
    else:
        def key(p: ProcSample) -> int:
            return p.memory.rss_bytes
    return tuple(sorted(proc_samples.values(), key=key, reverse=True)[:top_n])


def _sum_memory_info(items: Iterable[MemoryInfo]) -> MemoryInfo:
    """Sum MemoryInfo field-by-field.

    rss/vms/shared are always summed (which double-counts shared
    pages — this is a known property of RSS-summing and is why PSS
    exists as the opt-in correct alternative). uss/pss/swap are
    summed only when *every* contributing item has a non-None value;
    if any item is None, the aggregate for that field is None. This
    lets the caller distinguish "true tree PSS" from a partially-
    covered tree where AccessDenied forced fallbacks.
    """
    rss = vms = shared = 0
    uss = pss = swap = 0
    have_uss = have_pss = have_swap = True
    saw_any = False
    for m in items:
        saw_any = True
        rss += m.rss_bytes
        vms += m.vms_bytes
        shared += m.shared_bytes
        if m.uss_bytes is None:
            have_uss = False
        else:
            uss += m.uss_bytes
        if m.pss_bytes is None:
            have_pss = False
        else:
            pss += m.pss_bytes
        if m.swap_bytes is None:
            have_swap = False
        else:
            swap += m.swap_bytes
    # An empty input means we have no information at all — propagate
    # None for the optional fields rather than reporting zero coverage.
    if not saw_any:
        have_uss = have_pss = have_swap = False
    return MemoryInfo(
        rss_bytes=rss,
        vms_bytes=vms,
        shared_bytes=shared,
        uss_bytes=uss if have_uss else None,
        pss_bytes=pss if have_pss else None,
        swap_bytes=swap if have_swap else None,
    )


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
