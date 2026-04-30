# Add memory monitoring alongside CPU

## Background

The package currently monitors only CPU time across a process tree. CLAUDE.md
declares CPU as the strict scope and explicitly excludes memory. This spec
deliberately reverses that decision: memory becomes a first-class metric on
the same ticks, in the same `Sample`, behind the same `on_sample` callback.

The user's framing was "use the same approach as CPU." Structurally we can
match it (per-root sub-aggregates + global aggregate, top-N selection,
single-callback delivery, daemon-thread scheduler, no per-PID cache).
Semantically it differs in a way that affects the design and the docs:
**CPU times are cumulative monotonic counters; memory is a gauge.** Memory
has no `children_*`-equivalent in `/proc`, so a child's memory disappears
when it exits — short-lived processes' peak memory is invisible at any
sane tick rate. This is documented as a known limitation, parallel to the
CPU "born-and-die-between-ticks" caveat already in CLAUDE.md.

## Goals

- Each tick produces one `Sample` carrying both CPU and memory data,
  delivered to one callback.
- Memory aggregation supports a "correct" tree-level number (PSS) as an
  opt-in, and a cheap default (RSS + VMS) that always works.
- Top-N processes are reported separately by CPU and by memory so neither
  ranking criterion is fudged.
- All existing frozen design choices (no cache, no signal handlers, no
  auto-stop, single-thread scheduler, no Sink protocol) continue to hold.
- Breaking changes (rename of package, project, and public class) happen
  now while there is exactly one internal consumer.

## Non-goals

- Per-process peak memory between ticks (psutil exposes no such API and we
  explicitly don't cache).
- Memory thresholds or alerts.
- Per-cgroup memory accounting (future cgroup-v2 backend).
- Page-fault counters or oom-kill detection (caller's job).

## Renames

The package is no longer CPU-only. To avoid a misleading import path:

| Old | New |
| --- | --- |
| Project name (`pyproject.toml` `[project].name`) | `cpu-process-tree-monitor` → `process-tree-monitor` |
| Import package (`src/cpu_process_tree_monitor/`) | `process_tree_monitor` |
| Public class | `CpuTreeMonitor` → `ProcessTreeMonitor` |
| Sampler class | `PsutilTreeSampler` (unchanged — it samples the tree, not just CPU) |
| Models | `Sample`, `RootSample`, `ProcSample`, `CpuTimes` (unchanged) |
| New model | `MemoryInfo` |
| Public callback alias | `SampleCallback` (unchanged) |

Internal consumers updated in the same change:

- `docker_demo/runner.py`: import path and class name.
- `README.md`: install, quick-start, sample-schema references.
- `CLAUDE.md`: scope statement, frozen choices list, known-limitations
  section.

The directory `docker_demo/` keeps its name.

## Data model changes (`samples.py`)

A new pydantic model:

```python
class MemoryInfo(_Frozen):
    rss_bytes: int
    vms_bytes: int
    shared_bytes: int            # memory_info().shared
    # Populated only when full_memory_info=True AND the per-PID
    # memory_full_info() call succeeded. None on the per-PID fallback
    # path (AccessDenied, etc.).
    uss_bytes: int | None = None
    pss_bytes: int | None = None
    swap_bytes: int | None = None
```

`ProcSample` gains:

```python
memory: MemoryInfo
```

`RootSample` gains:

```python
memory_aggregate: MemoryInfo
# PIDs in this root that produced full_info data; None when the
# monitor is configured with full_memory_info=False.
memory_full_info_pid_count: int | None
```

`Sample` gains:

```python
memory_aggregate: MemoryInfo
memory_full_info_pid_count: int | None
top_processes_by_memory: tuple[ProcSample, ...]
top_processes_by_cpu: tuple[ProcSample, ...]   # renamed from top_processes
```

`top_processes` is removed in the same change. The only internal
consumer is `docker_demo/runner.py`, which today reads
`sample.top_processes[:3]`; it is updated to `sample.top_processes_by_cpu[:3]`
and gains a parallel `sample.top_processes_by_memory[:3]` line.

### Aggregation rules

`MemoryInfo` instances are summed field-by-field, with one twist for the
optional fields. For `uss_bytes`, `pss_bytes`, `swap_bytes`:

- If **every** contributing PID has a non-None value, the aggregate is the
  sum.
- If **any** contributing PID has `None`, the aggregate is `None`.

This makes "tree PSS" honest: it's only reported if the whole tree was
covered. The `memory_full_info_pid_count` field on `RootSample` and
`Sample` lets the caller see partial coverage explicitly.

`rss_bytes`, `vms_bytes`, `shared_bytes` are always summed. Their sums
double-count shared pages (libc mappings, copy-on-write, etc.). This is a
known property of RSS-summing and is documented; it's the reason PSS
exists as an opt-in.

## Sampler changes (`sampler.py`)

`PsutilTreeSampler.__init__` gains:

```python
def __init__(
    self,
    root_pids: Iterable[int],
    *,
    full_memory_info: bool = False,
) -> None:
```

The `full_memory_info` flag is stored on the instance and consulted in
`sample()`.

### `_read_proc_samples` split

Today `_read_proc_samples` reads CPU times and `comm` per PID. It is
extended (not split into separate top-level functions — staying close to
the existing single-pass shape) to also read a `MemoryInfo`.

Per-PID logic:

```
proc = psutil.Process(pid)
t = proc.cpu_times()           # existing
name = proc.name()             # existing
mem_info = proc.memory_info()  # new — cheap
if full_memory_info:
    try:
        full = proc.memory_full_info()
        # uss/pss/swap from full
    except psutil.AccessDenied:
        # fall back: keep mem_info; uss/pss/swap = None
        pass
```

Whole-PID `psutil.Error` (NoSuchProcess, ZombieProcess, generic Error)
continues to drop the PID from the sample, matching today's CPU
behavior. There is no asymmetry where memory failures would lose CPU
data or vice versa.

### New helper `_sum_memory_info`

Mirrors `_sum_cpu_times`:

```python
def _sum_memory_info(items: Iterable[MemoryInfo]) -> MemoryInfo:
    rss = vms = shared = 0
    uss = pss = swap = 0
    have_uss = have_pss = have_swap = True
    for m in items:
        rss += m.rss_bytes
        vms += m.vms_bytes
        shared += m.shared_bytes
        if m.uss_bytes is None: have_uss = False
        else: uss += m.uss_bytes
        if m.pss_bytes is None: have_pss = False
        else: pss += m.pss_bytes
        if m.swap_bytes is None: have_swap = False
        else: swap += m.swap_bytes
    return MemoryInfo(
        rss_bytes=rss, vms_bytes=vms, shared_bytes=shared,
        uss_bytes=uss if have_uss else None,
        pss_bytes=pss if have_pss else None,
        swap_bytes=swap if have_swap else None,
    )
```

### `_aggregate_per_root` extension

Adds `memory_aggregate` and `memory_full_info_pid_count` to each
`RootSample`. The count is `None` when `full_memory_info=False`; otherwise
it counts PIDs in `(root + descendants)` whose `MemoryInfo.pss_bytes` is
not None.

### `_aggregate_overall` extension

Same shape: returns the global memory aggregate and global full-info PID
count alongside the existing CPU aggregate.

### New `_select_top_processes_by_memory`

Ranks `proc_samples.values()` by:

- `pss_bytes` if `full_memory_info=True` (treating `None` as 0 — these
  are PIDs that hit the AccessDenied fallback and we don't want them
  pretending to dominate)
- else `rss_bytes`

Returns a `tuple[ProcSample, ...]` of length `top_n` (truncated). The
existing `_select_top_processes` is renamed `_select_top_processes_by_cpu`
for symmetry; behavior is unchanged.

### `sample()`

The orchestration is unchanged in shape — same five-step pipeline
(discover, read, per-root aggregate, overall aggregate, top selection),
just with two top lists instead of one and memory data flowing through
each step alongside CPU data.

## Monitor changes (`monitor.py`)

- Class renamed `CpuTreeMonitor` → `ProcessTreeMonitor`.
- New constructor parameter:

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
  ```

- `full_memory_info` is forwarded to `PsutilTreeSampler`.
- All lifecycle behavior is unchanged: daemon thread, private
  `schedule.Scheduler`, `start()`/`stop()` with `threading.Event`,
  context-manager protocol, restart-after-stop support, callback-error
  catch in `_tick`.
- No new threads. No new signal handling. No auto-stop on root exit.

## Public API (`__init__.py`)

```python
from .monitor import ProcessTreeMonitor, SampleCallback
from .sampler import PsutilTreeSampler
from .samples import CpuTimes, MemoryInfo, ProcSample, RootSample, Sample

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

## Demo runner (`docker_demo/runner.py`)

- Import path changes to `process_tree_monitor`.
- `ProcessTreeMonitor` instantiated with default `full_memory_info=False`.
- `emit_to_otel` adds a one-line memory section to its log output —
  `mem_rss=… mem_vms=… (uss=… pss=… if present)` — so the docker run
  output proves both metrics are flowing.

## Documentation

- **`README.md`**: update the install/usage examples to the new import,
  describe `MemoryInfo`, document the `full_memory_info` flag and its
  cost, mention the RSS-double-counting caveat and PSS as the opt-in fix.
- **`CLAUDE.md`**: rewrite the "Scope is strictly CPU" line to reflect
  CPU + memory; add to the frozen design choices that the memory side is
  a gauge (not cumulative), that `full_memory_info` is opt-in and
  defaults off, and that per-PID AccessDenied falls back to
  `memory_info()`. Extend the known-limitation section to call out:
  - Short-lived processes' peak memory is invisible (no `children_*`
    equivalent for memory).
  - Summing `rss_bytes` over a tree double-counts shared pages — that's
    why `pss_bytes` exists as an opt-in.

## Error handling

- `psutil.AccessDenied` from `memory_full_info()` → per-PID fallback to
  `memory_info()`. PSS/USS/swap are `None` for that PID. CPU still
  populated. No exception surfaces to the caller.
- Any other `psutil.Error` reading a PID → PID is dropped from the
  sample entirely (existing behavior; not memory-specific).
- Callback exceptions are caught and logged in `_tick`, just like today.

## Performance

- `full_memory_info=False` (default): one extra cheap psutil call
  (`memory_info()`, `/proc/<pid>/statm`) per PID per tick. Cost is
  comparable to today's `cpu_times()` call. AFL trees stay in low tens
  of PIDs; total per-tick cost is still well under 1 ms.
- `full_memory_info=True`: adds `memory_full_info()` per PID, which
  walks `/proc/<pid>/smaps`. Roughly 5–10× more expensive per PID. For a
  30-PID tree at 1 Hz this is still under ~10 ms per tick but the user
  should know it's there. Documented in README and CLAUDE.md.

## Verification (per CLAUDE.md conventions — no automated test suite)

- `docker build -t process-tree-monitor-demo -f docker_demo/Dockerfile .`
  must succeed. The Dockerfile is structurally unchanged: `pip install -e .`
  picks up the new project name from `pyproject.toml` automatically, and
  `COPY src/ ./src/` works regardless of the package directory name. Only
  the leading-comment block in the Dockerfile (build/run example commands
  and the stale JSONL reference) and the image tag in CLAUDE.md / README
  need updating.
- `docker run --rm -it process-tree-monitor-demo` must produce log lines
  showing both CPU and memory aggregates with non-zero values, and top
  processes including `afl-fuzz` and `target`.
- A second `docker run --rm -it process-tree-monitor-demo …
  --full-memory-info` (new optional CLI flag in the runner — easy to add
  alongside `--top-n`) must produce log lines where `pss` and `uss` are
  populated, and `memory_full_info_pid_count` reflects the tree size.

The runner gains a `--full-memory-info` boolean flag forwarded to
`ProcessTreeMonitor` so both code paths can be smoke-tested.

## Risk and rollout

- Single internal consumer, version 0.1.0, no external users — breaking
  rename is cheap now and only gets more painful later. Rollout is
  one PR.
- The biggest risk is the PSS-coverage subtlety: a partially-covered
  tree returns `pss_bytes=None` at the aggregate, which a naive caller
  might log as zero. The `memory_full_info_pid_count` field is the
  intended sanity check. README documents this.
