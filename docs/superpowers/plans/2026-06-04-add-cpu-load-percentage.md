# Recent CPU-load percentage (`cpu_percent`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a recent (per-tick) cores-busy CPU-load percentage — `cpu_percent` — at global, per-root, and per-process levels to every `Sample`, and replace the two top-N lists with a single flat per-process list.

**Architecture:** The sampler (`PsutilTreeSampler`) gains a tiny scalar cross-tick rate-state (`pid → (create_time, total_cpu_seconds)` plus the previous wall time). Each tick it diffs the current per-PID total CPU (`user+system+children_user+children_system`) against the stored value over the *measured* wall interval, attaches `cpu_percent` to each `ProcSample` via `model_copy`, and rolls those into per-root and global aggregates. Top-N selection is removed; `Sample.processes` now carries every sampled process. The rate-state is plain floats (never `psutil.Process` objects), rebuilt each tick, gated on `create_time` against PID reuse, and reset on monitor restart.

**Tech Stack:** Python 3.10+, `psutil`, `pydantic` v2 (frozen models, `model_copy(update=...)`), `schedule`. No test framework — this project has **no test suite** by convention; verification is pure-helper `python3 -c` checks plus a Docker smoke test.

**Spec:** `docs/superpowers/specs/2026-06-04-add-cpu-load-percentage-design.md`

---

## Conventions for this plan (read first)

- **No test files.** CLAUDE.md forbids a `tests/` directory and `pytest`. Verification steps use ad-hoc `PYTHONPATH=src python3 -c "..."` snippets that exercise pure helpers or run the sampler against the executor's *own* process tree. They create no files and add no dependencies.
- **`PYTHONPATH=src`** is used in every verification command so an editable install is not required.
- Tasks 1 and 2 are a **coupled pair**: Task 1 reshapes the `Sample` models and Task 2 rewrites the sampler that produces them. Between those two commits, `PsutilTreeSampler.sample()` would raise if called (it still references removed fields) — that is expected; the tree is consistent again after Task 2. Do them back-to-back.

## File structure

| File | Responsibility | Change |
| --- | --- | --- |
| `src/process_tree_monitor/samples.py` | pydantic data models | Add `cpu_percent` to `ProcSample`/`RootSample`/`Sample`; add `elapsed_s` + `processes` to `Sample`; remove `top_processes_by_cpu`/`top_processes_by_memory` |
| `src/process_tree_monitor/sampler.py` | tree walk + per-PID read + aggregation + rate computation | Full rewrite: rate-state + `reset()`, ordered discovery, `create_time` read, `cpu_percent` helpers, per-root/global rate aggregation, remove top-N selectors |
| `src/process_tree_monitor/monitor.py` | daemon-thread lifecycle | Remove `top_n`; call `sampler.reset()` in `start()`; drop `top_n=` from the `sample()` call |
| `docker_demo/runner.py` | OTel stand-in consumer | Remove `--top-n`; rewrite `emit_to_otel` to log `cpu_percent`/`elapsed_s` and a display-sorted `hot=[…]` from `processes` |
| `CLAUDE.md` | project memory | Amend frozen choices + known limitations |
| `README.md` | user docs | Document `cpu_percent`/`elapsed_s`/`processes`; drop top-N references |
| `src/process_tree_monitor/__init__.py` | public surface | **No change** — export list is unchanged (`reset()` is a new method on the already-exported `PsutilTreeSampler`) |

---

## Task 1: Reshape the `Sample` models

**Files:**
- Modify: `src/process_tree_monitor/samples.py`

- [ ] **Step 1: Replace the file contents**

Replace the entire contents of `src/process_tree_monitor/samples.py` with:

```python
"""Sample models."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class CpuTimes(_Frozen):
    user_seconds: float
    system_seconds: float
    children_user_seconds: float
    children_system_seconds: float


class MemoryInfo(_Frozen):
    rss_bytes: int
    vms_bytes: int
    shared_bytes: int
    # Populated only when full_memory_info=True AND the per-PID
    # memory_full_info() call succeeded. None on the per-PID fallback
    # path (AccessDenied, etc.).
    uss_bytes: int | None = None
    pss_bytes: int | None = None
    swap_bytes: int | None = None


class ProcSample(_Frozen):
    pid: int
    comm: str
    cpu_times: CpuTimes
    memory: MemoryInfo
    # Recent cores-busy CPU load for this PID over the last tick interval.
    # None until a baseline exists (first tick, restart, newly-appeared
    # PID, create_time mismatch). Filled in by the sampler via model_copy.
    cpu_percent: float | None = None


class RootSample(_Frozen):
    root_pid: int
    root_alive: bool
    descendant_count: int
    aggregate: CpuTimes
    memory_aggregate: MemoryInfo
    # Count of root+descendant PIDs whose memory.pss_bytes is populated.
    # None when the monitor was configured with full_memory_info=False.
    memory_full_info_pid_count: int | None
    # Sum of this root's contributing PIDs' cpu_percent. None when none of
    # them has a baseline yet.
    cpu_percent: float | None


class Sample(_Frozen):
    timestamp_unix: float
    interval_s: float
    # Measured wall seconds since the previous tick — the denominator
    # actually used for cpu_percent. None on the first tick / after restart.
    # Distinct from interval_s, which is the nominal configured interval.
    elapsed_s: float | None
    roots: tuple[RootSample, ...]
    process_count: int
    aggregate: CpuTimes
    memory_aggregate: MemoryInfo
    memory_full_info_pid_count: int | None
    # Global recent cores-busy CPU load (sum of per-PID cpu_percent). Can
    # exceed 100. None until a baseline exists.
    cpu_percent: float | None
    # Every sampled process, unranked, in tree-discovery order (roots
    # first). Replaces the former ranked top-N tuples.
    processes: tuple[ProcSample, ...]
```

- [ ] **Step 2: Verify the new model shape**

Run:

```bash
PYTHONPATH=src python3 -c "
from process_tree_monitor.samples import Sample, RootSample, ProcSample, CpuTimes, MemoryInfo
ct = CpuTimes(user_seconds=1.0, system_seconds=0.0, children_user_seconds=0.0, children_system_seconds=0.0)
mi = MemoryInfo(rss_bytes=1, vms_bytes=1, shared_bytes=0)
ps = ProcSample(pid=1, comm='x', cpu_times=ct, memory=mi)
assert ps.cpu_percent is None
rs = RootSample(root_pid=1, root_alive=True, descendant_count=0, aggregate=ct, memory_aggregate=mi, memory_full_info_pid_count=None, cpu_percent=None)
s = Sample(timestamp_unix=0.0, interval_s=1.0, elapsed_s=None, roots=(rs,), process_count=1, aggregate=ct, memory_aggregate=mi, memory_full_info_pid_count=None, cpu_percent=None, processes=(ps,))
assert not hasattr(s, 'top_processes_by_cpu') and not hasattr(s, 'top_processes_by_memory')
print('OK', s.cpu_percent, s.elapsed_s, len(s.processes))
"
```

Expected output: `OK None None 1`

- [ ] **Step 3: Commit**

```bash
git add src/process_tree_monitor/samples.py
git commit -m "feat: add cpu_percent/elapsed_s/processes to Sample models, drop top-N fields"
```

---

## Task 2: Rewrite the sampler to compute `cpu_percent`

**Files:**
- Modify: `src/process_tree_monitor/sampler.py`

- [ ] **Step 1: Replace the file contents**

Replace the entire contents of `src/process_tree_monitor/sampler.py` with:

```python
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
    whose PIDs are newly appeared). Because every PID in a tick shares one
    elapsed_s, summing the per-PID percentages equals (sum of deltas) /
    elapsed * 100 — the aggregate is exactly the sum of its parts.
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
```

- [ ] **Step 2: Verify the pure rate math in isolation**

Run:

```bash
PYTHONPATH=src python3 -c "
from process_tree_monitor.sampler import _total_cpu, _compute_cpu_percent, _aggregate_cpu_percent
from process_tree_monitor.samples import CpuTimes
# _total_cpu sums all four components
ct = CpuTimes(user_seconds=1.0, system_seconds=2.0, children_user_seconds=3.0, children_system_seconds=4.0)
assert _total_cpu(ct) == 10.0
# first tick: no prev or no elapsed -> None
assert _compute_cpu_percent(10.0, 100.0, None, 1.0) is None
assert _compute_cpu_percent(10.0, 100.0, (100.0, 5.0), None) is None
assert _compute_cpu_percent(10.0, 100.0, (100.0, 5.0), 0.0) is None
# normal delta: 0.5 cpu-seconds over 1.0s wall -> 50% (cores-busy)
assert _compute_cpu_percent(5.5, 100.0, (100.0, 5.0), 1.0) == 50.0
# multithreaded: 3.8 cpu-seconds over 1.0s -> 380%
assert _compute_cpu_percent(3.8, 100.0, (100.0, 0.0), 1.0) == 380.0
# PID reuse: create_time mismatch -> None
assert _compute_cpu_percent(5.5, 999.0, (100.0, 5.0), 1.0) is None
# negative delta -> None
assert _compute_cpu_percent(4.0, 100.0, (100.0, 5.0), 1.0) is None
# aggregate ignores None, sums the rest; all-None -> None
assert _aggregate_cpu_percent([None, None]) is None
assert _aggregate_cpu_percent([10.0, None, 5.0]) == 15.0
print('OK rate math')
"
```

Expected output: `OK rate math`

- [ ] **Step 3: Verify the sampler end-to-end against this process**

Run:

```bash
PYTHONPATH=src python3 -c "
import os, time
from process_tree_monitor import PsutilTreeSampler
s = PsutilTreeSampler([os.getpid()])
s1 = s.sample(interval_s=1.0)
assert s1.cpu_percent is None and s1.elapsed_s is None, ('tick1', s1.cpu_percent, s1.elapsed_s)
assert not hasattr(s1, 'top_processes_by_cpu')
assert len(s1.processes) >= 1 and s1.processes[0].cpu_percent is None
# burn CPU so the second tick sees a non-zero delta
t = time.time()
while time.time() - t < 0.3:
    pass
s2 = s.sample(interval_s=1.0)
assert s2.elapsed_s is not None and s2.elapsed_s > 0, ('elapsed', s2.elapsed_s)
assert s2.cpu_percent is not None and s2.cpu_percent > 0, ('load', s2.cpu_percent)
assert s2.processes[0].cpu_percent is not None
# reset() clears the baseline
s.reset()
s3 = s.sample(interval_s=1.0)
assert s3.cpu_percent is None and s3.elapsed_s is None
print('OK load=%.0f%% elapsed=%.2fs procs=%d' % (s2.cpu_percent, s2.elapsed_s, len(s2.processes)))
"
```

Expected output: `OK load=<N>% elapsed=<~0.3>s procs=<N>=` with a positive load and at least one process.

- [ ] **Step 4: Commit**

```bash
git add src/process_tree_monitor/sampler.py
git commit -m "feat: compute recent cpu_percent in the sampler, emit full process list"
```

---

## Task 3: Wire the monitor (remove `top_n`, reset on restart)

**Files:**
- Modify: `src/process_tree_monitor/monitor.py`

- [ ] **Step 1: Remove the `top_n` constructor parameter and field**

In `src/process_tree_monitor/monitor.py`, replace this `__init__`:

```python
    def __init__(
        self,
        root_pids: int | Iterable[int],
        on_sample: SampleCallback,
        *,
        interval_s: float = 1.0,
        top_n: int = 10,
        full_memory_info: bool = False,
    ) -> None:
        roots = [root_pids] if isinstance(root_pids, int) else list(root_pids)
        self._sampler = PsutilTreeSampler(roots, full_memory_info=full_memory_info)
        self._on_sample = on_sample
        self._interval_s = max(0.1, float(interval_s))
        self._top_n = int(top_n)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
```

with:

```python
    def __init__(
        self,
        root_pids: int | Iterable[int],
        on_sample: SampleCallback,
        *,
        interval_s: float = 1.0,
        full_memory_info: bool = False,
    ) -> None:
        roots = [root_pids] if isinstance(root_pids, int) else list(root_pids)
        self._sampler = PsutilTreeSampler(roots, full_memory_info=full_memory_info)
        self._on_sample = on_sample
        self._interval_s = max(0.1, float(interval_s))
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
```

- [ ] **Step 2: Reset the rate-state in `start()`**

Replace this `start()`:

```python
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        sched = schedule.Scheduler()
        sched.every(self._interval_s).seconds.do(self._tick)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(sched,),
            name="process-tree-monitor",
            daemon=True,
        )
        self._thread.start()
```

with:

```python
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # Reset the sampler's rate-state so a restart begins with no
        # baseline (first post-restart tick has cpu_percent / elapsed_s None).
        self._sampler.reset()
        sched = schedule.Scheduler()
        sched.every(self._interval_s).seconds.do(self._tick)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(sched,),
            name="process-tree-monitor",
            daemon=True,
        )
        self._thread.start()
```

- [ ] **Step 3: Drop `top_n=` from the `sample()` call**

Replace this `_tick()`:

```python
    def _tick(self) -> None:
        try:
            sample = self._sampler.sample(
                interval_s=self._interval_s, top_n=self._top_n,
            )
            self._on_sample(sample)
        except Exception:
            log.exception("sampler/callback error; continuing")
```

with:

```python
    def _tick(self) -> None:
        try:
            sample = self._sampler.sample(interval_s=self._interval_s)
            self._on_sample(sample)
        except Exception:
            log.exception("sampler/callback error; continuing")
```

- [ ] **Step 4: Verify the monitor delivers loads and resets on restart**

Run:

```bash
PYTHONPATH=src python3 -c "
import os, time
from process_tree_monitor import ProcessTreeMonitor
seen = []
m = ProcessTreeMonitor(os.getpid(), seen.append, interval_s=0.2)
m.start()
t = time.time()
while time.time() - t < 1.0:
    pass
m.stop()
assert seen, 'no samples delivered'
assert seen[0].cpu_percent is None, 'first tick should have no baseline'
assert any(s.cpu_percent is not None for s in seen[1:]), 'no load after first tick'
n0 = len(seen)
m.start()  # restart must reset the baseline
t = time.time()
while time.time() - t < 0.5:
    pass
m.stop()
assert len(seen) > n0, 'restart produced no samples'
assert seen[n0].cpu_percent is None, 'restart did not reset baseline'
print('OK samples=%d' % len(seen))
"
```

Expected output: `OK samples=<N>` with N at least ~5.

- [ ] **Step 5: Commit**

```bash
git add src/process_tree_monitor/monitor.py
git commit -m "feat: drop top_n from monitor, reset rate-state on start"
```

---

## Task 4: Update the demo runner

**Files:**
- Modify: `docker_demo/runner.py`

- [ ] **Step 1: Rewrite `emit_to_otel`**

In `docker_demo/runner.py`, replace the entire `emit_to_otel` function (from its `def` through the closing of its `log.info(...)` call) with:

```python
def emit_to_otel(sample: Sample) -> None:
    """Stand-in for an OpenTelemetry exporter call.

    In production this is where you'd record observable counters/gauges on
    a `metrics.Meter` (counters for cpu_times fields, gauges for memory
    fields and cpu_percent) and let the configured OTLP exporter push them.
    cpu_percent is a gauge (cores-busy, like top); the cpu_times second
    fields stay counters. cpu_percent / elapsed_s are None on the first
    tick (no baseline). For the smoke test we just log a compact summary so
    `docker run` output shows samples flowing.
    """
    log = logging.getLogger("otel")
    cpu = sample.aggregate
    mem = sample.memory_aggregate
    load_str = (
        f"{sample.cpu_percent:.0f}%" if sample.cpu_percent is not None else "n/a"
    )
    elapsed_str = (
        f"{sample.elapsed_s:.2f}s" if sample.elapsed_s is not None else "n/a"
    )
    # Display-only: the library no longer ranks processes, so the consumer
    # sorts the full list by recent load to show the hottest few.
    hot = sorted(
        sample.processes,
        key=lambda p: p.cpu_percent if p.cpu_percent is not None else -1.0,
        reverse=True,
    )[:3]
    top = ", ".join(
        f"{p.comm}({p.pid})="
        + (f"{p.cpu_percent:.0f}%" if p.cpu_percent is not None else "n/a")
        + f",rss={p.memory.rss_bytes // 1024}K"
        for p in hot
    )
    pss_str = (
        f"{mem.pss_bytes // 1024}K" if mem.pss_bytes is not None else "n/a"
    )
    log.info(
        "[otel] load=%s (u=%.1fs s=%.1fs cu=%.1fs cs=%.1fs) elapsed=%s | "
        "mem rss=%dK vms=%dK pss=%s pid_count=%s | procs=%d roots=%d hot=[%s]",
        load_str,
        cpu.user_seconds,
        cpu.system_seconds,
        cpu.children_user_seconds,
        cpu.children_system_seconds,
        elapsed_str,
        mem.rss_bytes // 1024,
        mem.vms_bytes // 1024,
        pss_str,
        sample.memory_full_info_pid_count,
        sample.process_count,
        len(sample.roots),
        top,
    )
```

- [ ] **Step 2: Remove the `--top-n` argument**

In `parse_args`, delete this line:

```python
    p.add_argument("--top-n", default=10, type=int)
```

(Leave `--interval`, `--max-runtime`, `--full-memory-info`, and the others untouched.)

- [ ] **Step 3: Drop `top_n=` from the monitor construction**

In `main`, replace:

```python
    monitor = ProcessTreeMonitor(
        root_pids=[afl.pid],
        on_sample=emit_to_otel,
        interval_s=args.interval,
        top_n=args.top_n,
        full_memory_info=args.full_memory_info,
    )
```

with:

```python
    monitor = ProcessTreeMonitor(
        root_pids=[afl.pid],
        on_sample=emit_to_otel,
        interval_s=args.interval,
        full_memory_info=args.full_memory_info,
    )
```

- [ ] **Step 4: Verify the runner imports, drops `--top-n`, and logs a load line**

Run:

```bash
PYTHONPATH=src python3 -c "
import sys, importlib.util
spec = importlib.util.spec_from_file_location('runner', 'docker_demo/runner.py')
runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
sys.argv = ['runner']
ns = runner.parse_args()
assert not hasattr(ns, 'top_n'), 'top_n arg still present'
from process_tree_monitor.samples import Sample, RootSample, ProcSample, CpuTimes, MemoryInfo
ct = CpuTimes(user_seconds=1.0, system_seconds=0.0, children_user_seconds=0.0, children_system_seconds=0.0)
mi = MemoryInfo(rss_bytes=2048, vms_bytes=4096, shared_bytes=0)
ps = ProcSample(pid=1, comm='afl-fuzz', cpu_times=ct, memory=mi, cpu_percent=42.0)
rs = RootSample(root_pid=1, root_alive=True, descendant_count=0, aggregate=ct, memory_aggregate=mi, memory_full_info_pid_count=None, cpu_percent=42.0)
s = Sample(timestamp_unix=0.0, interval_s=1.0, elapsed_s=1.0, roots=(rs,), process_count=1, aggregate=ct, memory_aggregate=mi, memory_full_info_pid_count=None, cpu_percent=42.0, processes=(ps,))
runner.emit_to_otel(s)
print('OK')
"
```

Expected output: an `[otel] load=42% (u=1.0s ...) elapsed=1.00s ... hot=[afl-fuzz(1)=42%,rss=2K]` line, then `OK`.

- [ ] **Step 5: Commit**

```bash
git add docker_demo/runner.py
git commit -m "feat: log cpu_percent/elapsed_s and a hot-by-load list in the demo runner"
```

---

## Task 5: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Amend the no-cache frozen bullet**

Replace:

```
- **`psutil` only; no per-PID cache.** Each tick walks
  `psutil.Process(root).children(recursive=True)` and reads `cpu_times()`
  and `memory_info()` per PID. CPU times are cumulative monotonic counters
  (OTel-counter-shaped). Memory is a gauge: instantaneous RSS/VMS/shared,
  with no `children_*` equivalent — short-lived processes' peak memory is
  invisible at any sane tick rate.
```

with:

```
- **`psutil` only; no per-PID *Process* cache.** Each tick walks
  `psutil.Process(root).children(recursive=True)` and reads `cpu_times()`
  and `memory_info()` per PID. CPU times are cumulative monotonic counters
  (OTel-counter-shaped). Memory is a gauge: instantaneous RSS/VMS/shared,
  with no `children_*` equivalent — short-lived processes' peak memory is
  invisible at any sane tick rate. The only cross-tick state is a scalar
  rate-state (pid → `(create_time, total_cpu_seconds)` plus the previous
  wall time) used solely to derive `cpu_percent`; it holds plain floats,
  not `psutil.Process` objects, is rebuilt every tick, is gated on
  `create_time` against PID reuse, and is cleared by `reset()`.
```

- [ ] **Step 2: Replace the Top-N frozen bullet with the load + full-list bullets**

Replace:

```
- **Top-N is reported per dimension.** `top_processes_by_cpu` (cumulative
  CPU) and `top_processes_by_memory` (PSS when full info is on, else RSS)
  are both on every `Sample`. No blended ranking.
```

with:

```
- **Recent CPU load is a derived gauge.** Each `Sample` carries
  `cpu_percent` (cores-busy, like `top`; can exceed 100) at global,
  per-root, and per-process levels, plus `elapsed_s`, the measured wall
  interval used as the denominator. Per-PID total = `user + system +
  children_user + children_system`, so a long-lived parent's load reflects
  short-lived children it reaps (AFL's fork-server reflects target
  executions). `None` until a baseline exists.
- **Full per-process list, unranked.** `Sample.processes` carries every
  sampled `ProcSample` (each with its `cpu_percent`) in tree-discovery
  order. There is no top-N and no ranking; consumers sort if they want to.
```

- [ ] **Step 3: Note the create_time exception in the "Removed" bullet**

Replace:

```
- **Removed and not coming back:** PID-reuse defense, explicit zombie
  check, drift-corrected scheduling, `schema_version`, `Sink` protocol,
  JSONL writer, watchdog handler.
```

with:

```
- **Removed and not coming back:** general PID-reuse defense (the narrow
  `create_time` gate on the `cpu_percent` rate-state is the one exception),
  explicit zombie check, drift-corrected scheduling, `schema_version`,
  `Sink` protocol, JSONL writer, watchdog handler.
```

- [ ] **Step 4: Add a known-limitation paragraph for recent load**

Replace:

```
A future cgroup v2 backend would close both gaps.
```

with:

```
**Recent load (`cpu_percent`):** `None` on the first tick and the first
tick after a restart (no baseline). Because per-PID totals include
`children_*`, a child that lives long enough to be sampled across several
ticks and is then reaped folds its accumulated CPU into its parent's
`children_*` in a single interval — a one-tick spike in that interval's
percentage. For AFL this is rare (targets are normally reaped between
ticks and never sampled). The integral is correct; only the per-interval
attribution spikes. `elapsed_s` exposes the denominator for sanity-checks.

A future cgroup v2 backend would close the born-and-die gaps.
```

- [ ] **Step 5: Update the demo-output expectation**

Replace:

```
Expect log lines from `emit_to_otel` with non-zero CPU and memory
aggregates and top processes (by CPU and by memory) including
`afl-fuzz` and `target`; AFL crashes within seconds. Add
`--full-memory-info` to populate PSS/USS/swap fields.
```

with:

```
Expect log lines from `emit_to_otel` with a non-zero `load=` percentage
(from the second tick on), `elapsed=` near the interval, CPU and memory
aggregates, and a `hot=[…]` list of the heaviest processes by recent load
including `afl-fuzz`; AFL crashes within seconds. The first line shows
`load=n/a` (no baseline yet). Add `--full-memory-info` to populate
PSS/USS/swap fields.
```

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record cpu_percent and full-process-list changes in CLAUDE.md"
```

---

## Task 6: Update README.md

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the quick-start `emit_to_otel`**

Replace:

```python
def emit_to_otel(sample: Sample) -> None:
    # Replace with a real OpenTelemetry meter / exporter in production.
    cpu = sample.aggregate
    mem = sample.memory_aggregate
    logging.getLogger("otel").info(
        "cpu u=%.1fs s=%.1fs | mem rss=%dK vms=%dK pss=%s | procs=%d",
        cpu.user_seconds, cpu.system_seconds,
        mem.rss_bytes // 1024, mem.vms_bytes // 1024,
        mem.pss_bytes if mem.pss_bytes is not None else "n/a",
        sample.process_count,
    )
```

with:

```python
def emit_to_otel(sample: Sample) -> None:
    # Replace with a real OpenTelemetry meter / exporter in production.
    # cpu_percent is a gauge (cores-busy, like top); the CpuTimes second
    # fields stay counters. cpu_percent / elapsed_s are None on the first
    # tick (no baseline to diff against).
    cpu = sample.aggregate
    mem = sample.memory_aggregate
    load = "n/a" if sample.cpu_percent is None else f"{sample.cpu_percent:.0f}%"
    logging.getLogger("otel").info(
        "load=%s cpu u=%.1fs s=%.1fs | mem rss=%dK vms=%dK pss=%s | procs=%d",
        load, cpu.user_seconds, cpu.system_seconds,
        mem.rss_bytes // 1024, mem.vms_bytes // 1024,
        mem.pss_bytes if mem.pss_bytes is not None else "n/a",
        sample.process_count,
    )
```

- [ ] **Step 2: Remove `top_n=10` from the quick-start monitor**

Replace:

```python
with ProcessTreeMonitor(
    root_pids=[afl.pid],
    on_sample=emit_to_otel,
    interval_s=1.0,
    top_n=10,
    full_memory_info=False,  # set True for USS/PSS/swap (slower)
) as monitor:
    afl.wait()
```

with:

```python
with ProcessTreeMonitor(
    root_pids=[afl.pid],
    on_sample=emit_to_otel,
    interval_s=1.0,
    full_memory_info=False,  # set True for USS/PSS/swap (slower)
) as monitor:
    afl.wait()
```

- [ ] **Step 3: Add a "Recent CPU load" section**

Replace:

```
## Memory fields and `full_memory_info`
```

with:

```
## Recent CPU load (`cpu_percent`)

Each `Sample` carries a recent CPU-load percentage at three levels —
`Sample.cpu_percent` (whole tree), `RootSample.cpu_percent` (per root),
and `ProcSample.cpu_percent` (per process). It is **cores-busy**, like
`top`: `delta_cpu_seconds / elapsed_s × 100`, where the per-PID delta uses
`user + system + children_user + children_system`. One fully-used core
reads ~100; a multithreaded process or a tree aggregate can exceed 100.

`Sample.elapsed_s` is the **measured** wall interval used as the
denominator (distinct from the nominal `interval_s`), so any percentage is
auditable. All of these are `None` until a baseline exists: the first tick,
the first tick after a `stop()`/`start()` restart, a newly-appeared PID, or
a PID whose `create_time` no longer matches (reuse).

Because per-PID totals include `children_*`, a long-lived process's load
reflects short-lived children it reaps — for AFL the fork-server's
`cpu_percent` reflects target-execution throughput even though no
individual target is ever directly sampled. The trade-off: a child that
lives long enough to be sampled across several ticks and is then reaped
folds its accumulated CPU into the parent's `children_*` in one interval, a
one-tick spike in that interval (rare for AFL; the integral stays correct).

## Memory fields and `full_memory_info`
```

- [ ] **Step 4: Replace the Sample-schema top-N paragraph**

Replace:

```
See `src/process_tree_monitor/samples.py` for the pydantic models;
`Sample.model_dump()` returns a plain dict suitable for use as
OpenTelemetry attributes, and `Sample.model_dump_json()` /
`Sample.model_json_schema()` are also available. Top-N processes are
exposed as two ranked tuples: `top_processes_by_cpu` (cumulative CPU)
and `top_processes_by_memory` (PSS when `full_memory_info=True`, else
RSS).
```

with:

```
See `src/process_tree_monitor/samples.py` for the pydantic models;
`Sample.model_dump()` returns a plain dict suitable for use as
OpenTelemetry attributes, and `Sample.model_dump_json()` /
`Sample.model_json_schema()` are also available. Every sampled process is
exposed, unranked, as `Sample.processes` — a tuple of `ProcSample` in
tree-discovery order (roots first), each carrying its `cpu_percent`,
`cpu_times`, and `memory`. There is no top-N; sort `processes` yourself if
you want the heaviest by any dimension.
```

- [ ] **Step 5: Add a recent-load limitation note**

Replace:

```
A cgroup v2 backend that captures cumulative CPU and current/peak
```

with:

```
**Recent load:** `cpu_percent` and `elapsed_s` are `None` on the first
tick and immediately after a restart. The `children_*`-inclusive total can
produce a one-tick spike when a multi-tick-lived child is finally reaped
(rare for AFL).

A cgroup v2 backend that captures cumulative CPU and current/peak
```

- [ ] **Step 6: Verify no stale references remain**

Run:

```bash
! grep -rn "top_processes\|top_n\|top-n" README.md CLAUDE.md src/ docker_demo/runner.py
```

Expected: no output, command exits 0 (the leading `!` inverts grep's exit code, so a clean tree succeeds).

- [ ] **Step 7: Commit**

```bash
git add README.md
git commit -m "docs: document cpu_percent/elapsed_s/processes in README"
```

---

## Task 7: Integration smoke test (Docker)

**Files:** none (verification only)

> Per CLAUDE.md, the user runs Docker. Ask the user to run these and paste output; do not run them yourself unless asked.

- [ ] **Step 1: Build**

Run:

```bash
docker build -t process-tree-monitor-demo -f docker_demo/Dockerfile .
```

Expected: build succeeds. The Dockerfile is unchanged — it `COPY`s `src/` and `runner.py` and `pip install -e .`, all unaffected by the rename-free changes.

- [ ] **Step 2: Run (default)**

Run:

```bash
docker run --rm -it process-tree-monitor-demo
```

Expected, from the **second** `[otel]` line onward:
- `load=` shows a plausible non-zero cores-busy percentage (first line shows `load=n/a`),
- `elapsed=` is close to the 1.0s interval,
- `hot=[…]` lists processes by recent load, including `afl-fuzz`,
- `procs=` matches the tree size; AFL crashes within seconds.

- [ ] **Step 3: Run (full memory info)**

Run:

```bash
docker run --rm -it process-tree-monitor-demo --full-memory-info
```

Expected: same as Step 2 plus non-`n/a` `pss=` values and a non-zero `pid_count=`.

- [ ] **Step 4: Final acceptance check**

Confirm against the spec's verification section: global `cpu_percent` non-`None` after tick 1, fork-server carrying high per-process load, `elapsed_s ≈ interval`, full process list present, memory path unaffected. If all hold, the feature is complete.

---

## Self-Review

**Spec coverage** (each spec section → task):
- Data model (`cpu_percent` ×3, `elapsed_s`, `processes`, top-N removed) → Task 1.
- Rate-state + `reset()` + `create_time` gate + ordered discovery + rate helpers + per-root/global aggregation + top-N selector removal → Task 2.
- Monitor `top_n` removal + `reset()` on `start()` → Task 3.
- Runner `--top-n` removal + `emit_to_otel` rewrite + gauge-vs-counter note → Task 4.
- CLAUDE.md frozen-choice amendments + known limitations → Task 5.
- README field docs + top-N removal → Task 6.
- Docker verification (incl. `--full-memory-info`) → Task 7.
- `__init__.py` unchanged (export list stable) → noted in File Structure.

**Placeholder scan:** none — every step shows full code or an exact command with expected output.

**Type/name consistency:** `cpu_percent` / `elapsed_s` / `processes` field names match across `samples.py`, `sampler.py`, `monitor.py`, and `runner.py`. Helper names (`_total_cpu`, `_compute_cpu_percent`, `_aggregate_cpu_percent`) are defined in Task 2 and used in the same file. `sample()` signature `(*, interval_s)` matches the monitor's call in Task 3. `reset()` defined in Task 2, called in Task 3.
