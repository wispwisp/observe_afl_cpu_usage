# Add Memory Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add psutil-based memory monitoring (RSS/VMS/shared by default; optional USS/PSS/swap via `memory_full_info`) into the same per-tick `Sample` as CPU, plus rename the package and class from `cpu-process-tree-monitor` → `process-tree-monitor`.

**Architecture:** One unified `ProcessTreeMonitor` produces one `Sample` per tick carrying both CPU and memory data, delivered to one callback. Memory is a gauge (not cumulative); aggregation across the tree uses RSS by default and PSS when the opt-in `full_memory_info` flag is on. Top-N processes are reported separately by CPU and by memory so neither ranking is fudged.

**Tech Stack:** Python 3.10+, `psutil`, `pydantic`, `schedule`, Linux-only (uses `/proc`). No pytest, no `tests/` directory — verification is by `python -c` smoke tests during development and by `docker build` / `docker run` of `docker_demo/` end-to-end.

**Spec:** [`docs/superpowers/specs/2026-04-30-add-memory-monitoring-design.md`](../specs/2026-04-30-add-memory-monitoring-design.md)

**Conventions for this plan:**
- The project deliberately has no automated test suite (CLAUDE.md). Each "test" step uses `python -c "..."` to import and exercise the code on the engineer's own PID. The final end-to-end smoke is `docker build` + `docker run`, which the engineer runs.
- Commit after each task. Use the existing `wisp <wispwis@gmail.com>` author identity inline (`git -c user.name=wisp -c user.email=wispwis@gmail.com commit ...`) — the repo has no committed git config.
- Co-Author trailer on every commit: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.

---

## File Structure

This plan touches the following files. The package directory is renamed in Task 1; later tasks reference the new path.

**Renamed (Task 1):**
- `src/cpu_process_tree_monitor/` → `src/process_tree_monitor/`
- `pyproject.toml` `[project].name`: `cpu-process-tree-monitor` → `process-tree-monitor`
- `monitor.py`: class `CpuTreeMonitor` → `ProcessTreeMonitor`

**Modified:**
- `src/process_tree_monitor/samples.py` — add `MemoryInfo`; extend `ProcSample`/`RootSample`/`Sample`; rename `top_processes` → `top_processes_by_cpu`; add `top_processes_by_memory`.
- `src/process_tree_monitor/sampler.py` — add `_sum_memory_info`; extend `_read_proc_samples` for memory; extend `_aggregate_per_root` and `_aggregate_overall` for memory; rename `_select_top_processes` → `_select_top_processes_by_cpu`; add `_select_top_processes_by_memory`; thread `full_memory_info` flag through `__init__` and `sample()`.
- `src/process_tree_monitor/monitor.py` — class rename; add `full_memory_info` constructor parameter; forward to sampler.
- `src/process_tree_monitor/__init__.py` — update class name and add `MemoryInfo` to re-exports.
- `docker_demo/runner.py` — update import and class name; add `--full-memory-info` argparse flag; extend `emit_to_otel` log line with memory fields.
- `docker_demo/Dockerfile` — update leading-comment block (build/run examples + stale JSONL reference).
- `README.md` — update install/usage examples; document `MemoryInfo`, the `full_memory_info` flag, the RSS-double-counting caveat, and PSS as the opt-in fix.
- `CLAUDE.md` — rewrite scope statement; update frozen-design-choices list; extend known-limitations section.

---

## Task 1: Rename package, class, and project

**Files:**
- Move: `src/cpu_process_tree_monitor/` → `src/process_tree_monitor/` (whole directory)
- Modify: `pyproject.toml` (project name)
- Modify: `src/process_tree_monitor/__init__.py` (docstring, exports — class rename only here)
- Modify: `src/process_tree_monitor/monitor.py` (class rename)
- Modify: `docker_demo/runner.py` (import + class instantiation)
- Modify: `docker_demo/Dockerfile` (leading-comment block — build tag, run tag, stale JSONL line)
- Modify: `README.md` (install + quick-start references; demo build/run commands)
- Modify: `CLAUDE.md` (demo build/run commands — scope rewrite happens in Task 9 once memory exists)

This is a mechanical, self-contained rename. The project compiles and the demo still works after this task; only memory is missing.

- [ ] **Step 1: Move the package directory**

```bash
git mv src/cpu_process_tree_monitor src/process_tree_monitor
```

- [ ] **Step 2: Update `pyproject.toml` project name**

Replace line 6:

```toml
name = "process-tree-monitor"
```

And line 8:

```toml
description = "CPU and memory usage monitor for a process tree given root PIDs"
```

- [ ] **Step 3: Rename class in `src/process_tree_monitor/monitor.py`**

Replace the docstring at the top:

```python
"""ProcessTreeMonitor: a daemon thread that ticks a PsutilTreeSampler via
schedule.Scheduler and dispatches each Sample to a single callback.
"""
```

Rename the class declaration (line 20):

```python
class ProcessTreeMonitor:
```

Rename the thread name (line 47) for clarity:

```python
name="process-tree-monitor",
```

Update the `__enter__` return type (line 58):

```python
def __enter__(self) -> "ProcessTreeMonitor":
```

- [ ] **Step 4: Update `src/process_tree_monitor/__init__.py`**

Replace entire file (preserves shape; only the docstring and class name change — `MemoryInfo` is added in Task 7):

```python
"""process-tree-monitor — CPU and memory usage monitor for a process tree given root PIDs."""
from .monitor import ProcessTreeMonitor, SampleCallback
from .sampler import PsutilTreeSampler
from .samples import CpuTimes, ProcSample, RootSample, Sample

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ProcessTreeMonitor",
    "PsutilTreeSampler",
    "CpuTimes",
    "ProcSample",
    "RootSample",
    "Sample",
    "SampleCallback",
]
```

- [ ] **Step 5: Update `docker_demo/runner.py`**

Change the import (line 17):

```python
from process_tree_monitor import ProcessTreeMonitor, Sample
```

Change the class instantiation (was line 94):

```python
    monitor = ProcessTreeMonitor(
        root_pids=[afl.pid],
        on_sample=emit_to_otel,
        interval_s=args.interval,
        top_n=args.top_n,
    )
```

(The `--full-memory-info` flag is added later in Task 8; this task changes only the rename.)

- [ ] **Step 6: Update Dockerfile leading comments**

In `docker_demo/Dockerfile`, replace the comment block at lines 1-12:

```dockerfile
# syntax=docker/dockerfile:1.6
#
# process-tree-monitor smoke-test image. Runs AFL++ against a deliberately
# crashing C++ target while the Python monitor watches AFL's pid tree and
# logs CPU + memory samples once per second.
#
# Build (from repo root, NOT from docker_demo/):
#   docker build -t process-tree-monitor-demo -f docker_demo/Dockerfile .
#
# Run:
#   docker run --rm -it process-tree-monitor-demo
#
```

(The stale "Outputs one JSONL sample per second to /out/cpu.jsonl" line is removed — JSONL output was deleted long ago per CLAUDE.md frozen-choices.)

- [ ] **Step 7: Update README.md**

Replace `cpu_process_tree_monitor` → `process_tree_monitor` and `CpuTreeMonitor` → `ProcessTreeMonitor` in the quick-start example (lines 21 and 38). Update the project title and description:

```markdown
# process-tree-monitor

CPU and memory usage monitor for a process tree given root PIDs. Originally
built to watch AFL++ fuzzing trees, but works for any subprocess tree. Each
tick the monitor produces one `Sample` and delivers it to a single
`on_sample` callback.
```

Update the demo build/run commands (lines 58-61):

```
docker build -t process-tree-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it process-tree-monitor-demo
```

(The detailed memory documentation is added in Task 9; this task only renames.)

- [ ] **Step 8: Update CLAUDE.md demo commands**

In CLAUDE.md, replace lines 36-38 (the demo build/run block) with:

```
docker build -t process-tree-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it process-tree-monitor-demo
```

(The scope statement rewrite happens in Task 9; this task only fixes the demo commands.)

- [ ] **Step 9: Smoke test the rename**

Run:

```bash
python -c "from process_tree_monitor import ProcessTreeMonitor, Sample, PsutilTreeSampler, CpuTimes, ProcSample, RootSample, SampleCallback; print('OK')"
```

Expected: `OK`

If the import fails because the package isn't installed in the current venv, the engineer (or user) needs to re-run `pip install -e .` in the dev venv. Per CLAUDE.md, the user runs `pip install`.

Run a sampler smoke test on the current process:

```bash
python -c "
import os
from process_tree_monitor import PsutilTreeSampler
s = PsutilTreeSampler([os.getpid()])
sample = s.sample(interval_s=1.0, top_n=3)
print(f'roots={len(sample.roots)} procs={sample.process_count} u={sample.aggregate.user_seconds:.3f}s')
"
```

Expected: a single line like `roots=1 procs=1 u=<small float>s`.

- [ ] **Step 10: Commit**

```bash
git add -A
git -c user.name=wisp -c user.email=wispwis@gmail.com commit -m "$(cat <<'EOF'
rename package: cpu-process-tree-monitor → process-tree-monitor

Mechanical rename to prepare for adding memory metrics alongside CPU.
Public class CpuTreeMonitor → ProcessTreeMonitor; package import path
cpu_process_tree_monitor → process_tree_monitor; project name in
pyproject.toml updated. No behavior change yet; the monitor still
samples only CPU.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `MemoryInfo` model and extend `Sample`/`RootSample`/`ProcSample`

**Files:**
- Modify: `src/process_tree_monitor/samples.py`

This task only adds the data shapes. The sampler still doesn't populate them — the next task does. Compilation must still succeed (the new fields use defaults / are added below the existing required fields so existing callers in the sampler don't break yet — see "interim defaults" note below).

**Interim defaults note:** During Tasks 2–4 the sampler hasn't been updated, so the existing `_aggregate_per_root` / `_aggregate_overall` / `sample()` would fail to construct `RootSample` / `Sample` without the new fields. To avoid temporary breakage between tasks, give the new memory fields *interim defaults* in this task that are removed (made required) in Task 5 once the sampler populates them. The `MemoryInfo` model itself has its three required `int` fields and three optional `int | None` fields with `= None` defaults — that part is final.

- [ ] **Step 1: Replace the contents of `src/process_tree_monitor/samples.py`**

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


_ZERO_MEMORY = MemoryInfo(rss_bytes=0, vms_bytes=0, shared_bytes=0)


class ProcSample(_Frozen):
    pid: int
    comm: str
    cpu_times: CpuTimes
    # Interim default; sampler populates this in Task 3. Made required in Task 5.
    memory: MemoryInfo = _ZERO_MEMORY


class RootSample(_Frozen):
    root_pid: int
    root_alive: bool
    descendant_count: int
    aggregate: CpuTimes
    # Interim defaults; sampler populates these in Task 4. Made required in Task 5.
    memory_aggregate: MemoryInfo = _ZERO_MEMORY
    memory_full_info_pid_count: int | None = None


class Sample(_Frozen):
    timestamp_unix: float
    interval_s: float
    roots: tuple[RootSample, ...]
    process_count: int
    aggregate: CpuTimes
    # Old field name kept temporarily so the existing sampler still constructs
    # a valid Sample. Renamed/split in Task 5 once the sampler is updated.
    top_processes: tuple[ProcSample, ...]
    # Interim defaults; sampler populates these in Task 4–5. Made required in Task 5.
    memory_aggregate: MemoryInfo = _ZERO_MEMORY
    memory_full_info_pid_count: int | None = None
    top_processes_by_cpu: tuple[ProcSample, ...] = ()
    top_processes_by_memory: tuple[ProcSample, ...] = ()
```

- [ ] **Step 2: Smoke test the new model**

```bash
python -c "
from process_tree_monitor.samples import MemoryInfo, ProcSample, RootSample, Sample, CpuTimes
m = MemoryInfo(rss_bytes=100, vms_bytes=200, shared_bytes=50)
print(f'mem default: pss={m.pss_bytes} rss={m.rss_bytes}')
m2 = MemoryInfo(rss_bytes=100, vms_bytes=200, shared_bytes=50, uss_bytes=80, pss_bytes=90, swap_bytes=0)
print(f'mem full: pss={m2.pss_bytes} uss={m2.uss_bytes}')
"
```

Expected:

```
mem default: pss=None rss=100
mem full: pss=90 uss=80
```

- [ ] **Step 3: Smoke test that the sampler still works (interim state)**

```bash
python -c "
import os
from process_tree_monitor import PsutilTreeSampler
s = PsutilTreeSampler([os.getpid()])
sample = s.sample(interval_s=1.0, top_n=3)
print(f'roots={len(sample.roots)} procs={sample.process_count} mem_rss={sample.memory_aggregate.rss_bytes}')
"
```

Expected: `roots=1 procs=1 mem_rss=0` — the new memory aggregate is the zero placeholder until Task 4 populates it.

- [ ] **Step 4: Commit**

```bash
git add src/process_tree_monitor/samples.py
git -c user.name=wisp -c user.email=wispwis@gmail.com commit -m "$(cat <<'EOF'
add MemoryInfo model and extend Sample/RootSample/ProcSample

Adds MemoryInfo (rss/vms/shared required, uss/pss/swap optional) and
embeds it in ProcSample/RootSample/Sample. Also adds top_processes_by_cpu
and top_processes_by_memory alongside the old top_processes for now;
the rename and field-required tightening happen once the sampler is
fully updated.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Read `memory_info()` per PID in the sampler

**Files:**
- Modify: `src/process_tree_monitor/sampler.py` (function `_read_proc_samples`)

This task adds the per-PID memory read but only the cheap `memory_info()` call (no opt-in `memory_full_info()` yet — that comes in Task 6). The new `MemoryInfo` is plugged into each `ProcSample`. Aggregation is still untouched.

- [ ] **Step 1: Update the import in `sampler.py`**

Change line 9:

```python
from .samples import CpuTimes, MemoryInfo, ProcSample, RootSample, Sample
```

- [ ] **Step 2: Replace `_read_proc_samples` (was lines 78-106)**

```python
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
```

- [ ] **Step 3: Smoke test that each ProcSample now carries memory**

```bash
python -c "
import os
from process_tree_monitor import PsutilTreeSampler
s = PsutilTreeSampler([os.getpid()])
sample = s.sample(interval_s=1.0, top_n=3)
for p in sample.top_processes:
    print(f'pid={p.pid} comm={p.comm} rss={p.memory.rss_bytes} vms={p.memory.vms_bytes} pss={p.memory.pss_bytes}')
"
```

Expected: one or more lines with non-zero `rss=<some bytes>` and `vms=<some bytes>` and `pss=None`.

- [ ] **Step 4: Commit**

```bash
git add src/process_tree_monitor/sampler.py
git -c user.name=wisp -c user.email=wispwis@gmail.com commit -m "$(cat <<'EOF'
read memory_info() per PID and attach to ProcSample

Each tick now reads psutil.Process.memory_info() per PID alongside
cpu_times(). RSS/VMS/shared are populated; uss/pss/swap remain None
until the opt-in full_memory_info path lands.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Per-root and overall memory aggregation

**Files:**
- Modify: `src/process_tree_monitor/sampler.py` (add `_sum_memory_info`, extend `_aggregate_per_root` and `_aggregate_overall`)

- [ ] **Step 1: Add `_sum_memory_info` helper at the bottom of `sampler.py`**

Add this function after `_sum_cpu_times`:

```python
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
```

- [ ] **Step 2: Replace `_aggregate_per_root` (was lines 109-133)**

```python
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
```

- [ ] **Step 3: Replace `_aggregate_overall` (was lines 136-144)**

```python
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
```

- [ ] **Step 4: Update `sample()` to wire the new aggregation outputs through**

Replace `sample()` (was lines 31-47). Note: `full_memory_info` is hardcoded to `False` here; Task 6 wires it through from the constructor. `top_processes` keeps the old name for now (renamed in Task 5).

```python
    def sample(self, *, interval_s: float, top_n: int) -> Sample:
        wall_now = time.time()
        per_root_descendants, all_pids = _discover_tree(self._root_pids)
        proc_samples = _read_proc_samples(all_pids)
        roots = _aggregate_per_root(per_root_descendants, proc_samples, full_memory_info=False)
        cpu_overall, mem_overall, full_info_count = _aggregate_overall(
            proc_samples, full_memory_info=False,
        )
        top = _select_top_processes(proc_samples, top_n)
        return Sample(
            timestamp_unix=wall_now,
            interval_s=interval_s,
            roots=roots,
            process_count=len(proc_samples),
            aggregate=cpu_overall,
            top_processes=top,
            memory_aggregate=mem_overall,
            memory_full_info_pid_count=full_info_count,
        )
```

- [ ] **Step 5: Smoke test that overall and per-root memory aggregates populate**

```bash
python -c "
import os
from process_tree_monitor import PsutilTreeSampler
s = PsutilTreeSampler([os.getpid()])
sample = s.sample(interval_s=1.0, top_n=3)
print(f'overall mem rss={sample.memory_aggregate.rss_bytes} pss={sample.memory_aggregate.pss_bytes} pid_count={sample.memory_full_info_pid_count}')
for r in sample.roots:
    print(f'  root pid={r.root_pid} alive={r.root_alive} mem_rss={r.memory_aggregate.rss_bytes} pss={r.memory_aggregate.pss_bytes} pid_count={r.memory_full_info_pid_count}')
"
```

Expected: `overall mem rss=<positive bytes> pss=None pid_count=None`, and one root line with similar shape and `alive=True`.

- [ ] **Step 6: Commit**

```bash
git add src/process_tree_monitor/sampler.py
git -c user.name=wisp -c user.email=wispwis@gmail.com commit -m "$(cat <<'EOF'
aggregate memory per root and overall

Adds _sum_memory_info helper with None-propagation rules for the
optional pss/uss/swap fields, and threads memory aggregates plus a
memory_full_info_pid_count through _aggregate_per_root and
_aggregate_overall. full_memory_info is hardcoded to False for now;
the constructor flag wires it through in a later commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Split top-N into `top_processes_by_cpu` and `top_processes_by_memory`; tighten Sample fields

**Files:**
- Modify: `src/process_tree_monitor/sampler.py` (rename + new selector + sample() rewrite)
- Modify: `src/process_tree_monitor/samples.py` (drop interim defaults; remove old `top_processes`)

This task removes the temporary `top_processes` field and the interim defaults from Task 2, making the new fields required.

- [ ] **Step 1: Rename `_select_top_processes` and add the memory selector in `sampler.py`**

Replace the existing `_select_top_processes` (was lines 147-166) with:

```python
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
```

- [ ] **Step 2: Update `sample()` in `sampler.py` to produce both top lists**

Replace the `sample()` body from Task 4 with:

```python
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
```

- [ ] **Step 3: Tighten `samples.py` — make new fields required, drop old `top_processes`**

Replace `src/process_tree_monitor/samples.py` with the final shape:

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


class RootSample(_Frozen):
    root_pid: int
    root_alive: bool
    descendant_count: int
    aggregate: CpuTimes
    memory_aggregate: MemoryInfo
    # Count of root+descendant PIDs whose memory.pss_bytes is populated.
    # None when the monitor was configured with full_memory_info=False.
    memory_full_info_pid_count: int | None


class Sample(_Frozen):
    timestamp_unix: float
    interval_s: float
    roots: tuple[RootSample, ...]
    process_count: int
    aggregate: CpuTimes
    memory_aggregate: MemoryInfo
    memory_full_info_pid_count: int | None
    top_processes_by_cpu: tuple[ProcSample, ...]
    top_processes_by_memory: tuple[ProcSample, ...]
```

- [ ] **Step 4: Smoke test both top lists populate**

```bash
python -c "
import os, subprocess, time
from process_tree_monitor import PsutilTreeSampler
# spawn a few children so top-N has something to sort
kids = [subprocess.Popen(['sleep', '5']) for _ in range(3)]
time.sleep(0.2)
s = PsutilTreeSampler([os.getpid()])
sample = s.sample(interval_s=1.0, top_n=3)
print(f'top_cpu len={len(sample.top_processes_by_cpu)} top_mem len={len(sample.top_processes_by_memory)}')
for p in sample.top_processes_by_memory:
    print(f'  by_mem pid={p.pid} comm={p.comm} rss={p.memory.rss_bytes}')
for k in kids: k.terminate()
for k in kids: k.wait()
"
```

Expected: `top_cpu len=3 top_mem len=3` and three lines under `by_mem` sorted by descending rss.

- [ ] **Step 5: Commit**

```bash
git add src/process_tree_monitor/samples.py src/process_tree_monitor/sampler.py
git -c user.name=wisp -c user.email=wispwis@gmail.com commit -m "$(cat <<'EOF'
split top-N into top_processes_by_cpu and top_processes_by_memory

Replaces the single top_processes field with two ranked lists: by CPU
(unchanged ranking) and by memory (RSS by default, PSS when
full_memory_info is on). Removes the interim default values from the
new sample fields, making them required.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Wire `full_memory_info` opt-in through sampler and monitor

**Files:**
- Modify: `src/process_tree_monitor/sampler.py` (constructor + `_read_proc_samples` + `sample()`)
- Modify: `src/process_tree_monitor/monitor.py` (constructor + forward to sampler)

- [ ] **Step 1: Add `full_memory_info` parameter to `PsutilTreeSampler.__init__`**

Replace `__init__` (was lines 20-29):

```python
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
```

- [ ] **Step 2: Update `_read_proc_samples` to accept and use the flag**

Replace the function from Task 3:

```python
def _read_proc_samples(
    pids: Iterable[int],
    full_memory_info: bool,
) -> dict[int, ProcSample]:
    """Read CPU times, comm, and memory info for each PID.

    With full_memory_info=False, only the cheap memory_info() is read
    per PID (RSS/VMS/shared). With full_memory_info=True, also
    memory_full_info() is read for USS/PSS/swap; that call walks
    /proc/<pid>/smaps and is ~5–10× more expensive. AccessDenied
    on memory_full_info() falls back to memory_info() for that one
    PID — uss/pss/swap come back as None for it. Other psutil.Error
    on any read drops the PID from the sample entirely (matches CPU
    behavior so enabling full_memory_info never loses CPU coverage).
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
    return proc_samples
```

- [ ] **Step 3: Update `sample()` to pass the flag through**

Replace the `sample()` body from Task 5 with:

```python
    def sample(self, *, interval_s: float, top_n: int) -> Sample:
        wall_now = time.time()
        per_root_descendants, all_pids = _discover_tree(self._root_pids)
        proc_samples = _read_proc_samples(all_pids, self._full_memory_info)
        roots = _aggregate_per_root(
            per_root_descendants, proc_samples, self._full_memory_info,
        )
        cpu_overall, mem_overall, full_info_count = _aggregate_overall(
            proc_samples, self._full_memory_info,
        )
        top_cpu = _select_top_processes_by_cpu(proc_samples, top_n)
        top_mem = _select_top_processes_by_memory(
            proc_samples, top_n, self._full_memory_info,
        )
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
```

- [ ] **Step 4: Add `full_memory_info` to `ProcessTreeMonitor.__init__`**

Replace the constructor in `monitor.py` (was lines 21-35):

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

- [ ] **Step 5: Smoke test default off and opt-in on**

```bash
python -c "
import os
from process_tree_monitor import PsutilTreeSampler
s = PsutilTreeSampler([os.getpid()])
sample = s.sample(interval_s=1.0, top_n=3)
print(f'default off: pss={sample.memory_aggregate.pss_bytes} pid_count={sample.memory_full_info_pid_count}')
"
```

Expected: `default off: pss=None pid_count=None`.

```bash
python -c "
import os
from process_tree_monitor import PsutilTreeSampler
s = PsutilTreeSampler([os.getpid()], full_memory_info=True)
sample = s.sample(interval_s=1.0, top_n=3)
print(f'opt-in on: pss={sample.memory_aggregate.pss_bytes} pid_count={sample.memory_full_info_pid_count}')
print(f'  proc[0] pss={sample.top_processes_by_memory[0].memory.pss_bytes} uss={sample.top_processes_by_memory[0].memory.uss_bytes}')
"
```

Expected: `opt-in on: pss=<positive int> pid_count=1`, with the per-PID line showing populated `pss` and `uss`.

- [ ] **Step 6: Commit**

```bash
git add src/process_tree_monitor/sampler.py src/process_tree_monitor/monitor.py
git -c user.name=wisp -c user.email=wispwis@gmail.com commit -m "$(cat <<'EOF'
add full_memory_info opt-in flag to sampler and monitor

PsutilTreeSampler and ProcessTreeMonitor gain a full_memory_info bool
constructor flag (default False). When on, _read_proc_samples calls
memory_full_info() per PID for USS/PSS/swap, with per-PID AccessDenied
fallback that leaves those fields as None. The default path stays
cheap; the opt-in path enables tree-correct PSS aggregates.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Re-export `MemoryInfo` from the package

**Files:**
- Modify: `src/process_tree_monitor/__init__.py`

- [ ] **Step 1: Replace `__init__.py`**

```python
"""process-tree-monitor — CPU and memory usage monitor for a process tree given root PIDs."""
from .monitor import ProcessTreeMonitor, SampleCallback
from .sampler import PsutilTreeSampler
from .samples import CpuTimes, MemoryInfo, ProcSample, RootSample, Sample

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ProcessTreeMonitor",
    "PsutilTreeSampler",
    "CpuTimes",
    "MemoryInfo",
    "ProcSample",
    "RootSample",
    "Sample",
    "SampleCallback",
]
```

- [ ] **Step 2: Smoke test the public surface**

```bash
python -c "from process_tree_monitor import ProcessTreeMonitor, PsutilTreeSampler, CpuTimes, MemoryInfo, ProcSample, RootSample, Sample, SampleCallback; print('public surface OK')"
```

Expected: `public surface OK`.

- [ ] **Step 3: Commit**

```bash
git add src/process_tree_monitor/__init__.py
git -c user.name=wisp -c user.email=wispwis@gmail.com commit -m "$(cat <<'EOF'
re-export MemoryInfo from package public surface

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Update `docker_demo/runner.py` to log memory and accept `--full-memory-info`

**Files:**
- Modify: `docker_demo/runner.py`

- [ ] **Step 1: Replace `emit_to_otel` to include a memory section**

Replace the function (was lines 20-45):

```python
def emit_to_otel(sample: Sample) -> None:
    """Stand-in for an OpenTelemetry exporter call.

    In production this is where you'd record observable counters/gauges on
    a `metrics.Meter` (counters for cpu_times fields, gauges for memory
    fields) and let the configured OTLP exporter push them. For the smoke
    test we just log a compact summary so `docker run` output shows
    samples flowing.
    """
    log = logging.getLogger("otel")
    cpu = sample.aggregate
    mem = sample.memory_aggregate
    top_cpu = ", ".join(
        f"{p.comm}({p.pid})="
        f"u={p.cpu_times.user_seconds:.1f}s,"
        f"s={p.cpu_times.system_seconds:.1f}s"
        for p in sample.top_processes_by_cpu[:3]
    )
    top_mem = ", ".join(
        f"{p.comm}({p.pid})="
        f"rss={p.memory.rss_bytes // 1024}K"
        + (f",pss={p.memory.pss_bytes // 1024}K" if p.memory.pss_bytes is not None else "")
        for p in sample.top_processes_by_memory[:3]
    )
    pss_str = (
        f"{mem.pss_bytes // 1024}K"
        if mem.pss_bytes is not None else "n/a"
    )
    log.info(
        "[otel] cpu u=%.1fs s=%.1fs cu=%.1fs cs=%.1fs | "
        "mem rss=%dK vms=%dK pss=%s pid_count=%s | procs=%d roots=%d "
        "top_cpu=[%s] top_mem=[%s]",
        cpu.user_seconds,
        cpu.system_seconds,
        cpu.children_user_seconds,
        cpu.children_system_seconds,
        mem.rss_bytes // 1024,
        mem.vms_bytes // 1024,
        pss_str,
        sample.memory_full_info_pid_count,
        sample.process_count,
        len(sample.roots),
        top_cpu,
        top_mem,
    )
```

- [ ] **Step 2: Add `--full-memory-info` to `parse_args`**

Replace `parse_args` (was lines 48-61):

```python
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="/out", type=Path)
    p.add_argument("--target", default="/opt/app/target/target")
    p.add_argument("--seeds", default="/opt/app/seeds")
    p.add_argument("--interval", default=1.0, type=float)
    p.add_argument("--top-n", default=10, type=int)
    p.add_argument(
        "--max-runtime",
        default=120,
        type=int,
        help="seconds before the runner terminates AFL itself",
    )
    p.add_argument(
        "--full-memory-info",
        action="store_true",
        help="enable USS/PSS/swap reads via memory_full_info() (slower)",
    )
    return p.parse_args()
```

- [ ] **Step 3: Forward the flag to `ProcessTreeMonitor`**

Replace the monitor instantiation (was lines 94-99):

```python
    monitor = ProcessTreeMonitor(
        root_pids=[afl.pid],
        on_sample=emit_to_otel,
        interval_s=args.interval,
        top_n=args.top_n,
        full_memory_info=args.full_memory_info,
    )
```

- [ ] **Step 4: Smoke test that the runner parses args**

```bash
python docker_demo/runner.py --help 2>&1 | head -20
```

Expected: argparse help text including a line for `--full-memory-info`.

- [ ] **Step 5: Commit**

```bash
git add docker_demo/runner.py
git -c user.name=wisp -c user.email=wispwis@gmail.com commit -m "$(cat <<'EOF'
runner: log memory aggregate and add --full-memory-info flag

emit_to_otel now logs an aggregated memory line (rss/vms/pss/pid_count)
alongside the CPU line, and a top_mem list parallel to top_cpu. The
new --full-memory-info CLI flag forwards through to ProcessTreeMonitor
so both code paths can be smoke-tested via docker run.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Update `README.md` and `CLAUDE.md` for the new scope

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Replace `README.md` body**

Replace the entire file with:

```markdown
# process-tree-monitor

CPU and memory usage monitor for a process tree given root PIDs. Originally
built to watch AFL++ fuzzing trees, but works for any subprocess tree. Each
tick the monitor produces one `Sample` and delivers it to a single
`on_sample` callback.

## Install

```
pip install -e .
```

Requires Python 3.10+, Linux (uses `/proc` via `psutil`), and `psutil`.

## Quick start

```python
import logging
import subprocess
from process_tree_monitor import ProcessTreeMonitor, Sample

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

afl = subprocess.Popen(
    ["afl-fuzz", "-i", "in", "-o", "out", "--", "./target"],
    start_new_session=True,
)

with ProcessTreeMonitor(
    root_pids=[afl.pid],
    on_sample=emit_to_otel,
    interval_s=1.0,
    top_n=10,
    full_memory_info=False,  # set True for USS/PSS/swap (slower)
) as monitor:
    afl.wait()
```

`root_pids` accepts a single int or a list of ints (parallel `-M`/`-S`).

## CPU vs memory: counters vs gauges

CPU times (`CpuTimes`) are *cumulative monotonic counters*: total user/system
seconds since process start, plus aggregated children-of-this-process times.
They are OTel-counter-shaped and survive child death because the kernel
adds a reaped child's CPU to its parent's `children_user`/`children_system`.

Memory (`MemoryInfo`) is a *gauge*: an instantaneous snapshot. There is no
`children_*` equivalent for memory, so when a process exits its memory is
gone — short-lived processes' peak memory is invisible at any sane tick rate.

## Memory fields and `full_memory_info`

The default `MemoryInfo` carries `rss_bytes`, `vms_bytes`, `shared_bytes`
from `psutil.Process.memory_info()`. These are cheap to read.

Setting `full_memory_info=True` additionally reads `memory_full_info()`,
which walks `/proc/<pid>/smaps` per PID (~5–10× more expensive) and
populates `uss_bytes`, `pss_bytes`, `swap_bytes`. On per-PID
`AccessDenied`, those three fields fall back to `None` for that PID
without losing CPU coverage.

**Why PSS matters:** summing `rss_bytes` across a process tree double-
counts shared pages (libc, copy-on-write). PSS (Proportional Set Size)
divides shared pages proportionally across the processes that share
them, so summing PSS over a tree gives a tree-correct memory number.
The `Sample.memory_aggregate.pss_bytes` is `None` if any contributing
PID had `pss_bytes=None` — partial coverage is honest. Use
`Sample.memory_full_info_pid_count` to see how many PIDs contributed.

## Sample schema

See `src/process_tree_monitor/samples.py` for the pydantic models;
`Sample.model_dump()` returns a plain dict suitable for use as
OpenTelemetry attributes, and `Sample.model_dump_json()` /
`Sample.model_json_schema()` are also available. Top-N processes are
exposed as two ranked tuples: `top_processes_by_cpu` (cumulative CPU)
and `top_processes_by_memory` (PSS when `full_memory_info=True`, else
RSS).

## Demo (Docker)

```
docker build -t process-tree-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it process-tree-monitor-demo
```

Inside the container `runner.py` launches AFL++ against a crashing
C++ target (`docker_demo/target/target.cc`), attaches the monitor, and
prints one `[otel] …` line per second standing in for a real OTel
exporter call. Add `--full-memory-info` to enable PSS/USS/swap:

```
docker run --rm -it process-tree-monitor-demo --full-memory-info
```

## Limitations

**CPU:** the born-and-die-between-ticks gap is largely closed by reading
`cpu_times()`'s `children_user` / `children_system` on a still-alive
parent: when the kernel reaps a child, its CPU is added to the parent's
`children_*`. AFL's master and fork-server stay alive throughout a run,
so target-binary CPU is captured via the fork-server even though no
individual target is ever directly sampled. Remaining narrow gap: an
intermediate parent that exits while a child is still alive reparents
that child to init, whose `children_*` we don't sample.

**Memory:** there is no `children_*`-equivalent for memory, so a process
that is born and exits between ticks contributes nothing to any sample.
Short-lived processes' peak memory is invisible. Tree aggregates of
`rss_bytes` double-count shared pages; `pss_bytes` (opt-in) is the
correct tree-summable metric.

A cgroup v2 backend that captures cumulative CPU and current/peak
memory regardless of process lifecycle is on the roadmap; the sampler
interface is kept clean so it slots in behind the same
`ProcessTreeMonitor` API.
```

- [ ] **Step 2: Update `CLAUDE.md`**

Replace lines 1-5 (the title block) with:

```markdown
# process-tree-monitor — project memory for future Claude sessions

## What this project is

A Python subsystem that monitors CPU and memory usage of an AFL++ fuzzing
process and its descendant tree. Drops into existing infrastructure that
already launches AFL++ via `subprocess`. Each tick produces one `Sample`
and delivers it to a single `on_sample` callback supplied by the caller;
the production target is OpenTelemetry.
```

Replace the "Frozen design choices" section (was around lines 11-34) with:

```markdown
## Frozen design choices

These were chosen deliberately and survived simplification passes.
Do not reintroduce removed pieces without asking the user.

- **`psutil` only; no per-PID cache.** Each tick walks
  `psutil.Process(root).children(recursive=True)` and reads `cpu_times()`
  and `memory_info()` per PID. CPU times are cumulative monotonic counters
  (OTel-counter-shaped). Memory is a gauge: instantaneous RSS/VMS/shared,
  with no `children_*` equivalent — short-lived processes' peak memory is
  invisible at any sane tick rate.
- **`full_memory_info` is opt-in and defaults off.** When on, each tick
  also reads `memory_full_info()` (walks `/proc/<pid>/smaps`, ~5–10× more
  expensive) for USS/PSS/swap. Per-PID `AccessDenied` falls back to
  `memory_info()` for that PID — uss/pss/swap come back as `None`. The
  per-tick `memory_full_info_pid_count` lets the caller detect partial
  coverage.
- **Single `on_sample` callback.** No `Sink` protocol, no fanout, no JSONL
  writer, no watchdog tailing. Failures inside the callback are caught
  and logged. The OTel adapter lives in the caller — see
  `docker_demo/runner.py:emit_to_otel` for the stand-in.
- **Multi-root is first-class.** `root_pids: list[int]`; `Sample` carries
  per-root sub-aggregates (CPU and memory) plus a global aggregate
  (parallel `-M`/`-S`).
- **Top-N is reported per dimension.** `top_processes_by_cpu` (cumulative
  CPU) and `top_processes_by_memory` (PSS when full info is on, else RSS)
  are both on every `Sample`. No blended ranking.
- **No cgroup backend.** Sampler interface is kept clean so cgroup v2
  can be added later without API churn.
- **Lifecycle: one daemon thread driving a private `schedule.Scheduler()`.**
  `stop()` sets a `threading.Event` and joins. Restart after `stop()` is
  supported. No auto-stop on root exit — the runner owns that.
- **Library installs no signal handlers.** The runner owns SIGINT/SIGTERM
  and calls `monitor.stop()`.
- **Removed and not coming back:** PID-reuse defense, explicit zombie
  check, drift-corrected scheduling, `schema_version`, `Sink` protocol,
  JSONL writer, watchdog handler.
- **Memory-related out of scope:** thresholds/alerts, per-process peak
  memory between ticks, page-fault counters, oom-kill detection.
```

Replace the "Known limitation" section (was around lines 36-44) with:

```markdown
## Known limitations

**CPU:** the born-and-die-between-ticks gap is largely closed by
`cpu_times()`'s `children_user` / `children_system`: when the kernel
reaps a child, its CPU is added to the still-alive parent's `children_*`.
AFL's master and fork-server stay alive throughout, so target-binary CPU
is captured via the fork-server. Remaining narrow gap: an intermediate
parent that exits while a child is still alive reparents that child to
init, which we don't sample.

**Memory:** no `children_*`-equivalent exists, so a process that is born
and exits between ticks contributes nothing to any memory sample.
Short-lived processes' peak memory is invisible. Tree aggregates of
`rss_bytes` double-count shared pages — `pss_bytes` (opt-in via
`full_memory_info=True`) is the correct tree-summable metric.

A future cgroup v2 backend would close both gaps.
```

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git -c user.name=wisp -c user.email=wispwis@gmail.com commit -m "$(cat <<'EOF'
docs: rewrite README and CLAUDE.md for CPU + memory scope

Documents the new MemoryInfo model, the full_memory_info opt-in flag
(cost, AccessDenied fallback, partial-coverage semantics), the
counter-vs-gauge distinction between CPU and memory, and the
RSS-double-counting caveat that motivates PSS. Updates the frozen
design choices and known limitations to match.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: End-to-end Docker smoke test (engineer/user runs)

**Files:** none modified. Verification only.

Per CLAUDE.md, the user runs `docker build` and `docker run`. The agent should ask the user to perform these steps and paste back the output.

- [ ] **Step 1: Ask the user to run the default-mode build and run**

Provide these commands and ask the user to paste the relevant log lines:

```bash
docker build -t process-tree-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it process-tree-monitor-demo
```

Expected: `[otel]` log lines once per second showing both `cpu u=...` and `mem rss=...K vms=...K pss=n/a pid_count=None`, with `top_cpu=[afl-fuzz(...), target(...)]` and `top_mem=[...]` populated. The `pss=n/a` and `pid_count=None` confirm full-info is off by default. AFL should crash within seconds.

- [ ] **Step 2: Ask the user to run the opt-in mode**

```bash
docker run --rm -it process-tree-monitor-demo --full-memory-info
```

Expected: `[otel]` log lines now showing `pss=<positive>K pid_count=<positive int>`, top_mem entries with `pss=<int>K` populated, and similar AFL crash behavior. The `pid_count` should be roughly equal to `procs=` in the same line (perfect coverage when no AccessDenied).

- [ ] **Step 3: Confirm acceptance**

If both runs produce the expected log shape with non-zero memory aggregates, the implementation is verified end-to-end. If the `--full-memory-info` run shows `pss=n/a` despite the flag, investigate whether the container is missing `CAP_SYS_PTRACE` (rare in Docker by default; if so, re-run with `--cap-add=SYS_PTRACE`).

- [ ] **Step 4: Push branch (engineer/user, on approval)**

```bash
git push -u origin use_cpu_times
```

(Or open a PR per the user's normal workflow.)
