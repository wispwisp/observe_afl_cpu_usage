# Extract `sample()` phases into named functions — design

## Goal

Refactor `PsutilTreeSampler.sample()` in
`src/cpu_process_tree_monitor/sampler.py` so each of its four
comment-labeled phases becomes one or more named module-level
functions (Phase 4, which currently bundles two distinct steps —
cross-tree aggregate and top-N selection — splits into two). The
function names should carry the structure the comments currently
carry, making `sample()` itself a short, top-down readable
orchestrator.

This is a pure-internal restructuring. No public API change, no
behavior change.

## Scope

**In scope:** rewriting `src/cpu_process_tree_monitor/sampler.py`
in place.

**Out of scope:**
- `src/cpu_process_tree_monitor/__init__.py` (re-exports unchanged)
- `src/cpu_process_tree_monitor/monitor.py` (calls `.sample()` unchanged)
- `src/cpu_process_tree_monitor/samples.py` (Pydantic models unchanged)
- `docker_demo/runner.py`
- `CLAUDE.md`, `README.md`, `pyproject.toml`

## Public surface — unchanged

- `PsutilTreeSampler.__init__(root_pids: Iterable[int])` — same
- `PsutilTreeSampler.sample(*, interval_s: float, top_n: int) -> Sample`
  — same signature, same returned `Sample` shape.
- The pre-existing `_sum_cpu_times(items) -> CpuTimes` module helper
  stays as-is.

## New shape of `sample()`

After the refactor, `sample()` reads top-down as a sequence of named
calls:

```python
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
```

The `wall_now` inline comment is the only inline comment that survives
inside `sample()` — it explains a non-obvious *why* (one timestamp for
the whole Sample, decoupled from per-PID work duration) that no
function name can carry.

## Five new module-level helpers

All are private (underscore-prefixed), live in
`src/cpu_process_tree_monitor/sampler.py`, and have a one-line or short
docstring describing the *why* (not the *what*). Concrete signatures
and responsibilities:

### `_discover_tree`

```python
def _discover_tree(
    root_pids: list[int],
) -> tuple[dict[int, list[int]], set[int]]:
```

For each root, calls `psutil.Process(root).children(recursive=True)`
and collects the descendants. Returns `(per_root_descendants, all_pids)`
where `all_pids` is the union of roots and all descendants.

Returning `all_pids` here (rather than letting the caller derive it)
avoids a second pass over the dict in `sample()` and keeps the union
construction adjacent to the walk that produced it.

A root that is dead/gone/inaccessible (`psutil.Error`) contributes
an empty descendant list; the root pid is still added to `all_pids`
so `_read_proc_samples` will attempt it and `_aggregate_per_root`
can later report `root_alive=False`.

Docstring carries the why: dead-root → empty-list contract; and that
short-lived grandchildren born and reaped between ticks are missed
here but their CPU survives via the still-alive parent's `children_*`.

### `_read_proc_samples`

```python
def _read_proc_samples(pids: Iterable[int]) -> dict[int, ProcSample]:
```

For each pid, opens a fresh `psutil.Process`, reads `cpu_times()` and
`name()`, builds a `ProcSample`. PIDs that vanish between
`_discover_tree` and this read are silently dropped.

Docstring carries the why: `cpu_times()` is cumulative-since-start so
a fresh `psutil.Process` is correct without priming (unlike
`cpu_percent`); `comm` is truncated to 15 chars to match the
`/proc/<pid>/comm` kernel limit; the vanish-race drop is intentional.

### `_aggregate_per_root`

```python
def _aggregate_per_root(
    per_root_descendants: dict[int, list[int]],
    proc_samples: dict[int, ProcSample],
) -> tuple[RootSample, ...]:
```

For each root, builds one `RootSample` containing
`root_alive=root_pid in proc_samples`, `descendant_count=len(desc)`,
and `aggregate=_sum_cpu_times(...)` over `(root + descendants)` PIDs
that produced a sample.

Iteration order over `per_root_descendants` preserves the caller's
input order (Python 3.9 dict semantics + insertion order preserved by
`__init__`), which matters for AFL `-M`/`-S` where the main fuzzer is
conventionally first.

Docstring carries the why: a zombie/already-reaped root yields
`root_alive=False` but its surviving descendants still contribute, so
the caller sees leftover work in the tree even after the root exits.

### `_aggregate_overall`

```python
def _aggregate_overall(
    proc_samples: dict[int, ProcSample],
) -> CpuTimes:
```

Sums `CpuTimes` across `proc_samples.values()`. Auto-deduped by pid via
the dict keys (a descendant shared between two roots is counted once).

Docstring carries the why: dedup semantics, and that this is a
cross-tree aggregate distinct from per-root sub-aggregates.

### `_select_top_processes`

```python
def _select_top_processes(
    proc_samples: dict[int, ProcSample],
    top_n: int,
) -> tuple[ProcSample, ...]:
```

Returns the top-N hottest `ProcSample`s by cumulative lifetime CPU
(sum of `user + system + children_user + children_system`).

Docstring carries the why: "top by total cumulative" — the AFL master
will usually dominate because it's been running longest. This is *not*
"top by current rate".

## Comments policy

The current `sample()` has heavy inline `# Phase N — ...` comments
explaining each block. Three categories:

1. **Phase headlines** (`# Phase 1 - tree discovery.`, etc.) — deleted.
   The function names replace them.
2. **What-it-does prose** (e.g. "psutil performs the /proc walk") —
   deleted. Project convention is to default to no comments.
3. **Why-it-is-this-way prose** (race semantics, kernel /proc/comm
   limit, cumulative vs. rate, zombie-root behavior, dedup-by-dict) —
   moved into short docstrings on the relevant new function. Each
   docstring is one sentence or a short paragraph.

The single inline comment kept inside `sample()` is the `wall_now`
explanation, since it spans the whole function and no helper carries
it.

## Behavior preservation checklist

- Same `psutil.Process(...).children(recursive=True)` call per root.
- Same `psutil.Error` handling: `pass` in `_discover_tree`,
  `continue` in `_read_proc_samples`. No new exception types caught,
  none silenced that weren't before.
- Same `comm[:15]` truncation.
- Same dedup-by-dict semantics for the cross-tree aggregate.
- Same sort key for top-N (sum of all four `CpuTimes` fields).
- Same `Sample` field assembly and field order.
- Same handling of dead/inaccessible roots
  (`root_alive=False`, descendants from earlier ticks are not
  remembered — there is no cache).
- `_sum_cpu_times` is reused unchanged by `_aggregate_per_root` and
  `_aggregate_overall`.

## Verification

Per `CLAUDE.md`, there is no automated test suite. Verification is:

1. `docker build -t cpu-process-tree-monitor-demo -f docker_demo/Dockerfile .`
2. `docker run --rm -it cpu-process-tree-monitor-demo`
3. Confirm `emit_to_otel` log lines still show non-zero aggregate CPU
   and top processes including `afl-fuzz` and `target`.

The user runs these — Claude does not run `docker build` / `docker run`
unless asked.

Because the refactor is mechanical (extract method → free function;
move comments → docstrings), the diff should be reviewable line-by-line
against the original four-phase structure.

## Acceptance criteria

- `PsutilTreeSampler.sample()` body is ≤ 20 lines and contains no
  `Phase N` comments.
- Five new module-level helpers exist with the names and signatures
  above.
- Each new helper has a docstring carrying the *why* it had as an
  inline comment in the original.
- No edits to `__init__.py`, `monitor.py`, `samples.py`, or any file
  outside `src/cpu_process_tree_monitor/sampler.py`.
- `git diff --stat` after the edit shows only `sampler.py` modified.
- Demo run still produces the expected `emit_to_otel` output (verified
  by the user).
