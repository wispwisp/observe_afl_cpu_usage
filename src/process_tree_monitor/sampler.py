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

    A small scalar rate-state (pid -> (create_time, total_cpu_seconds) from
    the previous tick, plus the previous wall time) is kept so each tick can
    derive a recent CPU-load percentage (cpu_percent). This is NOT a
    psutil.Process cache: it stores plain floats, is rebuilt every tick, is
    immune to PID reuse (each delta is gated on a matching create_time), and
    is cleared by reset().
    """

    def __init__(
        self,
        root_pids: Iterable[int],
        *,
        full_memory_info: bool = False,
    ) -> None:
        # Coerce to int, dedupe while preserving insertion order (so
        # per-root output keeps the order the caller passed, which
        # matters for AFL -M/-S where the main fuzzer is conventionally
        # first), and reject empty here rather than letting sample()
        # silently produce a useless aggregate later.
        deduped = list(dict.fromkeys(int(p) for p in root_pids))
        if not deduped:
            raise ValueError("root_pids must be non-empty")
        self._root_pids: list[int] = deduped
        self._full_memory_info: bool = bool(full_memory_info)
        # Cross-tick rate-state for cpu_percent: pid -> (create_time,
        # total_cpu_seconds). Plain scalars only; rebuilt every tick.
        self._prev_total_cpu: dict[int, tuple[float, float]] = {}
        self._prev_wall: float | None = None

    def reset(self) -> None:
        """Drop the cross-tick rate-state so the next sample() has no
        baseline: cpu_percent and elapsed_s come back None on that tick.
        ProcessTreeMonitor.start() calls this so a restart begins clean.
        """
        self._prev_total_cpu = {}
        self._prev_wall = None

    def sample(self, *, interval_s: float) -> Sample:
        wall_now = time.time()
        per_root_descendants, ordered_pids = _discover_tree(self._root_pids)
        proc_samples, create_times = _read_proc_samples(
            ordered_pids, self._full_memory_info,
        )

        if self._prev_wall is not None and wall_now > self._prev_wall:
            elapsed_s: float | None = wall_now - self._prev_wall
        else:
            elapsed_s = None

        # Enrich each ProcSample with its recent-load cpu_percent and
        # rebuild the rate-state from this tick's totals. Iterating a key
        # snapshot (list(...)) so reassigning existing keys is unambiguous;
        # reassignment preserves insertion (discovery) order.
        new_state: dict[int, tuple[float, float]] = {}
        for pid in list(proc_samples):
            ps = proc_samples[pid]
            total_now = _total_cpu(ps.cpu_times)
            create_now = create_times[pid]
            pct = _compute_cpu_percent(
                total_now, create_now, self._prev_total_cpu.get(pid), elapsed_s,
            )
            proc_samples[pid] = ps.model_copy(update={"cpu_percent": pct})
            new_state[pid] = (create_now, total_now)
        self._prev_total_cpu = new_state
        self._prev_wall = wall_now

        roots = _aggregate_per_root(
            per_root_descendants, proc_samples, self._full_memory_info,
        )
        cpu_overall, mem_overall, full_info_count = _aggregate_overall(
            proc_samples, self._full_memory_info,
        )
        overall_cpu_percent = _aggregate_cpu_percent(
            p.cpu_percent for p in proc_samples.values()
        )
        return Sample(
            timestamp_unix=wall_now,
            interval_s=interval_s,
            elapsed_s=elapsed_s,
            roots=roots,
            process_count=len(proc_samples),
            aggregate=cpu_overall,
            memory_aggregate=mem_overall,
            memory_full_info_pid_count=full_info_count,
            cpu_percent=overall_cpu_percent,
            processes=tuple(proc_samples.values()),
        )


def _discover_tree(
    root_pids: list[int],
) -> tuple[dict[int, list[int]], list[int]]:
    """Walk each root's descendant tree via psutil.

    Returns the per-root descendant lists and a single de-duplicated,
    order-preserving list of every PID to sample (each root first, then its
    descendants as children(recursive=True) returned them). A dead/gone/
    inaccessible root contributes an empty descendant list; its root_pid is
    still included so _read_proc_samples will attempt it and
    _aggregate_per_root can report root_alive=False. children(recursive=True)
    walks the tree at call time, so short-lived grandchildren born and reaped
    between ticks are missed here, but their CPU is still captured via their
    (still-alive) parent's children_*.
    """
    per_root_descendants: dict[int, list[int]] = {}
    ordered_pids: list[int] = []
    for root_pid in root_pids:
        desc: list[int] = []
        try:
            proc = psutil.Process(root_pid)
            desc = [d.pid for d in proc.children(recursive=True)]
        except psutil.Error:
            pass
        per_root_descendants[root_pid] = desc
        ordered_pids.append(root_pid)
        ordered_pids.extend(desc)
    # Dedupe while preserving first-seen (discovery) order: a PID shared
    # between two roots appears once, at its first occurrence.
    ordered_pids = list(dict.fromkeys(ordered_pids))
    return per_root_descendants, ordered_pids


def _read_proc_samples(
    pids: Iterable[int],
    full_memory_info: bool,
) -> tuple[dict[int, ProcSample], dict[int, float]]:
    """Read CPU times, comm, memory info, and create_time for each PID.

    Returns the ProcSample map (cpu_percent left at its None default; it is
    filled later in sample()) and a parallel pid -> create_time map used to
    gate the cpu_percent delta against PID reuse. create_time is kept out of
    ProcSample on purpose — it is an internal detail of the rate-state.

    With full_memory_info=False, only the cheap memory_info() is read per PID
    (RSS/VMS/shared). With full_memory_info=True, also memory_full_info() is
    read for USS/PSS/swap; that call walks /proc/<pid>/smaps and is ~5-10x
    more expensive. AccessDenied on memory_full_info() falls back to
    memory_info() for that one PID — uss/pss/swap come back as None for it.
    Other psutil.Error on any read drops the PID from the sample entirely
    (matches CPU behavior so enabling full_memory_info never loses CPU
    coverage). comm is truncated to 15 chars to match /proc/<pid>/comm.
    """
    proc_samples: dict[int, ProcSample] = {}
    create_times: dict[int, float] = {}
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            t = proc.cpu_times()
            name = proc.name()
            mem = proc.memory_info()
            create = proc.create_time()
        except psutil.Error:
            continue

        uss = pss = swap = None
        if full_memory_info:
            try:
                full = proc.memory_full_info()
                uss = full.uss
                pss = full.pss
                # On Linux psutil exposes swap on full_info; guard for safety.
                swap = getattr(full, "swap", None)
            except psutil.AccessDenied:
                pass
            except psutil.Error:
                # NoSuchProcess/ZombieProcess between calls — drop the PID.
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
                uss_bytes=uss,
                pss_bytes=pss,
                swap_bytes=swap,
            ),
        )
        create_times[pid] = create
    return proc_samples, create_times


def _aggregate_per_root(
    per_root_descendants: dict[int, list[int]],
    proc_samples: dict[int, ProcSample],
    full_memory_info: bool,
) -> tuple[RootSample, ...]:
    """Build one RootSample per root.

    For each root, sum CpuTimes and MemoryInfo over (root + descendants) PIDs
    that produced a sample, and sum their cpu_percent into a per-root recent
    load. A zombie/already-reaped root yields root_alive=False but its
    surviving descendants still contribute. memory_full_info_pid_count is None
    when full_memory_info is off; otherwise it counts root+descendant PIDs
    whose memory.pss_bytes is not None.
    """
    out: list[RootSample] = []
    for root_pid, desc in per_root_descendants.items():
        contributing = [
            proc_samples[p] for p in (root_pid, *desc) if p in proc_samples
        ]
        cpu_agg = _sum_cpu_times(p.cpu_times for p in contributing)
        mem_agg = _sum_memory_info(p.memory for p in contributing)
        cpu_pct = _aggregate_cpu_percent(p.cpu_percent for p in contributing)
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
            cpu_percent=cpu_pct,
        ))
    return tuple(out)


def _aggregate_overall(
    proc_samples: dict[int, ProcSample],
    full_memory_info: bool,
) -> tuple[CpuTimes, MemoryInfo, int | None]:
    """Sum CpuTimes and MemoryInfo across all sampled PIDs.

    Auto-deduped by pid via the dict keys: a descendant shared between two
    roots is counted once. Returns (cpu_agg, mem_agg, full_info_pid_count).
    full_info_pid_count is None when full_memory_info is off; otherwise it
    counts PIDs whose memory.pss_bytes is not None. The overall cpu_percent
    is computed by the caller from the enriched proc_samples.
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


def _total_cpu(c: CpuTimes) -> float:
    """Total cumulative CPU seconds for one process: its own user+system
    plus the user+system of children it has reaped. Including children_*
    means a long-lived parent's recent load reflects short-lived children
    it reaps — for AFL the fork-server's load reflects target executions.
    """
    return (
        c.user_seconds
        + c.system_seconds
        + c.children_user_seconds
        + c.children_system_seconds
    )


def _compute_cpu_percent(
    total_now: float,
    create_now: float,
    prev: tuple[float, float] | None,
    elapsed_s: float | None,
) -> float | None:
    """Cores-busy recent CPU load for one PID, or None when there is no
    usable baseline.

    prev is (create_time, total_cpu_seconds) from the previous tick. Returns
    None on the first tick (prev is None or elapsed_s is None or <= 0), on
    PID reuse (the stored create_time no longer matches), and on a negative
    delta (counter/clock anomaly). The result is cores-busy: 100.0 == one
    core fully busy over the interval; a multithreaded process or a tree can
    exceed 100.
    """
    if elapsed_s is None or elapsed_s <= 0 or prev is None:
        return None
    prev_create, prev_total = prev
    if prev_create != create_now:
        return None
    delta = total_now - prev_total
    if delta < 0:
        return None
    return delta / elapsed_s * 100.0


def _aggregate_cpu_percent(percents: Iterable[float | None]) -> float | None:
    """Sum the per-PID cpu_percent values that have a baseline.

    Returns None when none of them do (the first tick, or a root all of
    whose PIDs are newly appeared). When only a subset has a baseline (a mix
    of returning and newly-appeared PIDs), the sum covers only that subset
    and under-reports the true load for that tick. Because every PID in a
    tick shares one elapsed_s, summing the per-PID percentages equals
    (sum of deltas) / elapsed * 100 — the aggregate is exactly the sum of
    its parts.
    """
    vals = [p for p in percents if p is not None]
    if not vals:
        return None
    return sum(vals)


def _sum_memory_info(items: Iterable[MemoryInfo]) -> MemoryInfo:
    """Sum MemoryInfo field-by-field.

    rss/vms/shared are always summed (which double-counts shared pages —
    this is a known property of RSS-summing and is why PSS exists as the
    opt-in correct alternative). uss/pss/swap are summed only when *every*
    contributing item has a non-None value; if any item is None, the
    aggregate for that field is None. This lets the caller distinguish
    "true tree PSS" from a partially-covered tree where AccessDenied forced
    fallbacks.
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
    """Sum CpuTimes field-by-field across the given processes."""
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
