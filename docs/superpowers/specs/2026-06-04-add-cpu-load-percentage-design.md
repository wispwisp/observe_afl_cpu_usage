# Add recent CPU-load percentage (`cpu_percent`)

## Background

The package reports CPU usage only as **cumulative seconds** — `CpuTimes`
carries `user_seconds`, `system_seconds`, `children_user_seconds`,
`children_system_seconds`, all monotonic counters since each process
started. Nothing divides those seconds by elapsed time, so there is no
**load percentage** anywhere. `Sample.interval_s` is the *configured
nominal* tick interval, not measured elapsed time, so it is not even a
usable denominator.

A load percentage is inherently a **delta**:
`(cpu_seconds_now − cpu_seconds_prev) / (wall_now − wall_prev)`. The
sampler is deliberately stateless across ticks ("no per-PID cache" — a
frozen choice) and builds a fresh `psutil.Process` each tick, so it never
computes a delta. psutil's own `Process.cpu_percent()` keeps per-PID state
between calls, which is exactly the per-PID caching the frozen choice
avoided. This spec adds the metric while being deliberate about where the
small amount of cross-tick state lives.

This work was scoped through a series of decisions with the user:

1. **Unit:** cores-busy, like `top`/`htop` (`delta / elapsed × 100`). One
   fully-used core = 100%; a multi-core tree can exceed 100%. Not
   normalized to machine core count.
2. **Window:** recent (per-tick delta), not a lifetime average.
3. **Scope:** per-process **and** per-root **and** global.
4. **Components:** per-PID total = `user + system + children_user +
   children_system`, so a long-lived process's percentage reflects the
   CPU of short-lived children it has reaped — for AFL this captures
   target-execution throughput via the fork-server.
5. **Architecture:** the sampler owns the rate-state (Approach A below).
6. **Top-N removed:** the two top-N lists are removed entirely and
   replaced by a single flat list of every sampled process.

## Goals

- Each `Sample` carries a recent CPU-load percentage at three levels:
  global, per-root, and per-process.
- The denominator is the **measured** wall interval, exposed as
  `elapsed_s` so every percentage is auditable.
- Per-process data is delivered for the whole tree (a flat unranked
  list), not a ranked subset.
- The cross-tick state is a dict of scalars (never `psutil.Process`
  objects), immune to PID reuse and bounded to the currently-sampled
  PIDs.
- All other frozen choices (no signal handlers, no auto-stop,
  single-thread scheduler, no `Sink` protocol, memory semantics) continue
  to hold.

## Non-goals

- Lifetime-average load (total CPU / total elapsed since start).
- Normalized-to-machine percentage (dividing by `os.cpu_count()`).
- Exposing `create_time` on `ProcSample` (kept internal to the rate
  state).
- Any ranking / top-N / "hottest now" list — removed, not re-added in a
  new form.

## Frozen choices being reversed (with user authorization)

CLAUDE.md says removed/frozen pieces must not return without asking. The
user explicitly requested each of these in the design dialogue:

| Frozen choice | Change |
| --- | --- |
| "no per-PID cache" | Amended to **no per-PID *Process* cache**. A `dict[pid → (create_time, prev_total_cpu_seconds)]` of scalars is kept solely to compute `cpu_percent`, rebuilt each tick, reset on restart. |
| "Top-N is reported per dimension" | **Removed.** Replaced by a full per-process list `Sample.processes`. |
| "No 'top by current rate' / No blended ranking" | **Moot.** There is no ranking of any kind anymore. |

The sampler's `sample()` is also no longer a pure function of `/proc` at
call time: it depends on the previous call. This is intrinsic to a
delta-based metric and mirrors psutil's own first-call convention
(first call has no baseline). Documented below.

## Data model changes (`samples.py`)

`CpuTimes` and `MemoryInfo` are unchanged.

`ProcSample` gains:

```python
cpu_percent: float | None = None
```

`RootSample` gains:

```python
cpu_percent: float | None
```

`Sample` gains and loses:

```python
elapsed_s: float | None                 # NEW — measured wall delta (rate denominator)
cpu_percent: float | None               # NEW — global recent load
processes: tuple[ProcSample, ...]       # NEW — every sampled ProcSample, discovery order
# REMOVED: top_processes_by_cpu, top_processes_by_memory
```

`process_count` stays; it equals `len(processes)` and remains a cheap
convenience for callers that only want the count.

### `cpu_percent` semantics

- **Unit:** cores-busy. `delta_total_cpu_seconds / elapsed_s × 100`. A
  single-threaded process pinning one core reads ~100; a process spread
  across cores, or a tree aggregate, can exceed 100.
- **`None`** (uniform across `ProcSample`, `RootSample`, `Sample`) means
  *no baseline*: the first tick, the first tick after a `stop()`/`start()`
  restart, a PID that did not appear in the previous tick, a PID whose
  `create_time` no longer matches the stored one (PID reuse), or a
  non-positive `elapsed_s`.

### `elapsed_s` semantics

The **measured** `wall_now − prev_wall` actually used as the denominator,
in seconds. `None` on the first tick (and first tick after restart).
Distinct from `interval_s`, which remains the nominal configured interval.

### `processes` ordering

Tree-discovery order: each root first, then its descendants in the order
`children(recursive=True)` returned them. To make this deterministic,
`_discover_tree` returns an ordered PID list instead of relying on
`set` iteration order (see below). Unranked.

## Sampler changes (`sampler.py`)

### Rate-state

`PsutilTreeSampler` gains private state:

```python
self._prev_total_cpu: dict[int, tuple[float, float]]  # pid -> (create_time, total_cpu_seconds)
self._prev_wall: float | None
```

Initialized empty / `None` in `__init__`. A new method:

```python
def reset(self) -> None:
    self._prev_total_cpu = {}
    self._prev_wall = None
```

This is the amended "no per-PID cache" choice: scalars only, never
`psutil.Process` objects. It cannot grow unbounded — every tick it is
replaced wholesale with the currently-sampled PIDs — and it is immune to
PID reuse because each delta is gated on a matching `create_time`.

### Per-PID `create_time`

`create_time` is needed to gate deltas against PID reuse. It is read
during the per-PID walk (`proc.create_time()`, cheap — already in
`/proc/<pid>/stat`) and carried only far enough to compute the delta and
to rebuild `_prev_total_cpu`. It is **not** added to `ProcSample`.

Implementation note: `_read_proc_samples` returns its existing
`dict[int, ProcSample]` plus a parallel `dict[int, float]` of
`create_time` per PID (or an equivalent internal carrier). `ProcSample`
construction is unchanged except `cpu_percent` defaults to `None` there;
it is filled in the rate step.

### Rate computation

After reading proc samples and before aggregation:

```
wall_now = time.time()
elapsed  = wall_now - prev_wall   if prev_wall is not None and wall_now > prev_wall else None

for each sampled pid:
    total_now = user + system + children_user + children_system
    prev_create, prev_total = self._prev_total_cpu.get(pid, (None, None))
    if elapsed is not None and prev_create == create_time_now:
        delta = total_now - prev_total
        cpu_percent = delta / elapsed * 100   if delta >= 0 else None
    else:
        cpu_percent = None
```

Each frozen `ProcSample` is enriched with its `cpu_percent` via
`model_copy(update={"cpu_percent": value})`. The enriched samples are what
flow into the aggregates and the `processes` list, so every consumer sees
consistent numbers.

After computing, rebuild state:

```
self._prev_total_cpu = {pid: (create_time_now, total_now) for each sampled pid}
self._prev_wall = wall_now
```

### Aggregate percentages

Global and per-root `cpu_percent` are computed from the **per-PID deltas**,
not by re-differencing a summed cumulative:

```
agg_cpu_percent = (sum of delta for contributing PIDs with a valid delta) / elapsed * 100
                = sum of those PIDs' cpu_percent
```

`None` when `elapsed` is `None` or no contributing PID has a valid delta.
Computing from deltas (rather than `sum(total_now) − sum(total_prev)`)
keeps the aggregate equal to the sum of its parts and prevents membership
changes (a PID appearing or disappearing) from producing a spurious
spike or a negative value.

### Removed selection

`_select_top_processes_by_cpu` and `_select_top_processes_by_memory` are
deleted. `sample()` loses its `top_n` parameter and returns
`processes=tuple(enriched_proc_samples_in_discovery_order)`.

### `_discover_tree` ordering

`_discover_tree` currently returns `all_pids: set[int]`. To give
`processes` a deterministic, meaningful order (roots first, then
descendants as walked), it returns an **ordered** structure instead — an
ordered list of PIDs (de-duplicated, preserving first-seen order) is
threaded through `_read_proc_samples` so the resulting dict — and the
`processes` tuple built from it — is in discovery order. The per-root
descendant map is unchanged.

## Monitor changes (`monitor.py`)

- Remove the `top_n` constructor parameter and the `self._top_n` field;
  drop `top_n=` from the `self._sampler.sample(...)` call.
- `start()` calls `self._sampler.reset()` before launching the thread, so
  a restart after `stop()` begins with no baseline (first post-restart
  tick has `cpu_percent=None` / `elapsed_s=None`).
- All other lifecycle behavior is unchanged: daemon thread, private
  `schedule.Scheduler`, `threading.Event` stop, context-manager protocol,
  callback-error catch in `_tick`.

## Public API (`__init__.py`)

No change to the export list — `Sample`, `RootSample`, `ProcSample`,
`CpuTimes`, `MemoryInfo`, `ProcessTreeMonitor`, `PsutilTreeSampler`,
`SampleCallback` are all still exported. `PsutilTreeSampler.reset()` is a
new public method on an already-exported class.

## Demo runner (`docker_demo/runner.py`)

- Remove the `--top-n` argument and stop passing `top_n=`.
- `emit_to_otel`:
  - Log the global `cpu_percent` and `elapsed_s` alongside the existing
    CPU-seconds and memory lines.
  - Replace the `top_processes_by_cpu` / `top_processes_by_memory` log
    sections with a compact view derived from `sample.processes`. For
    human readability the runner may sort a few entries by `cpu_percent`
    for display — this sorting is a presentation choice in the consumer,
    not a library feature.
  - Docstring note: `cpu_percent` maps to an OTel **gauge** (e.g.
    `ObservableGauge`), distinct from the cumulative-second **counters**.

## Error handling

- Reuse the existing per-PID `psutil.Error` handling: a PID that fails to
  read is dropped from the sample (unchanged). A dropped PID simply has no
  entry in the new state, so the next tick treats it as no-baseline.
- A negative delta (should not happen for a matched live PID, but guards
  against clock or counter anomalies) yields `cpu_percent=None` for that
  PID, and the PID is excluded from the aggregate sum.
- Callback exceptions remain caught and logged in `_tick`.

## Known limitations (to document in CLAUDE.md)

- **First tick / restart:** every `cpu_percent` and `elapsed_s` is `None`
  on the first tick and on the first tick after a restart — there is no
  baseline to diff against.
- **Reaped-sampled-child spike:** including `children_*` means a child
  that lives long enough to be sampled across several ticks and is then
  reaped contributes its accumulated CPU to its parent's `children_*` in a
  single interval, producing a one-tick spike in that interval's
  percentage. For AFL this is rare (targets are normally born-and-reaped
  between ticks and never sampled; a multi-second hanging target is the
  exception). The integral is correct; only the per-interval attribution
  spikes. This is inherent to any rate built on a reap-accounted counter.
- **Born-and-die gap (carried over):** a process born and reaped entirely
  between two ticks never appears as its own PID; its CPU is captured only
  insofar as a still-alive ancestor reaped it into `children_*`.

## Verification (per CLAUDE.md conventions — no automated test suite)

- `docker build -t process-tree-monitor-demo -f docker_demo/Dockerfile .`
  must succeed.
- `docker run --rm -it process-tree-monitor-demo` must show, from the
  **second** tick onward:
  - non-`None` global `cpu_percent` with a plausible cores-busy value,
  - the fork-server PID carrying a high per-process `cpu_percent`,
  - `elapsed_s` ≈ the configured interval,
  - `processes` listing the whole tree (count matches `process_count`),
  - the first tick showing `cpu_percent`/`elapsed_s` as `None` / not-yet.
- A second run with `--full-memory-info` must still populate USS/PSS/swap
  as before — the memory path is untouched.

## Risk and rollout

- Single internal consumer (`docker_demo/runner.py`), version 0.1.0, no
  external users — removing top-N and reshaping `Sample` is cheap now.
  One PR.
- The main subtlety is the reaped-sampled-child spike; it is documented
  as a known limitation, and `elapsed_s` lets a consumer sanity-check the
  denominator behind any surprising percentage.
