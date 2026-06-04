# process-tree-monitor — project memory for future Claude sessions

## What this project is

A Python subsystem that monitors CPU and memory usage of an AFL++ fuzzing
process and its descendant tree. Drops into existing infrastructure that
already launches AFL++ via `subprocess`. Each tick produces one `Sample`
and delivers it to a single `on_sample` callback supplied by the caller;
the production target is OpenTelemetry.

## Frozen design choices

These were chosen deliberately and survived simplification passes.
Do not reintroduce removed pieces without asking the user.

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
- **No cgroup backend.** Sampler interface is kept clean so cgroup v2
  can be added later without API churn.
- **Lifecycle: one daemon thread driving a private `schedule.Scheduler()`.**
  `stop()` sets a `threading.Event` and joins. Restart after `stop()` is
  supported. No auto-stop on root exit — the runner owns that.
- **Library installs no signal handlers.** The runner owns SIGINT/SIGTERM
  and calls `monitor.stop()`.
- **Removed and not coming back:** general PID-reuse defense (the narrow
  `create_time` gate on the `cpu_percent` rate-state is the one exception),
  explicit zombie check, drift-corrected scheduling, `schema_version`,
  `Sink` protocol, JSONL writer, watchdog handler.
- **Memory-related out of scope:** thresholds/alerts, per-process peak
  memory between ticks, page-fault counters, oom-kill detection.

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

**Recent load (`cpu_percent`):** `None` on the first tick and the first
tick after a restart (no baseline). Because per-PID totals include
`children_*`, a child that lives long enough to be sampled across several
ticks and is then reaped folds its accumulated CPU into its parent's
`children_*` in a single interval — a one-tick spike in that interval's
percentage. For AFL this is rare (targets are normally reaped between
ticks and never sampled). The integral is correct; only the per-interval
attribution spikes. `elapsed_s` exposes the denominator for sanity-checks.

A future cgroup v2 backend would close the born-and-die gaps.

## How to run the demo

```
docker build -t process-tree-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it process-tree-monitor-demo
```

Expect log lines from `emit_to_otel` with a non-zero `load=` percentage
(from the second tick on), `elapsed=` near the interval, CPU and memory
aggregates, and a `hot=[…]` list of the heaviest processes by recent load
including `afl-fuzz`; AFL crashes within seconds. The first line shows
`load=n/a` (no baseline yet). Add `--full-memory-info` to populate
PSS/USS/swap fields.

## Conventions

- **No automated test suite.** No `tests/` directory, no `pytest` in
  dependencies, no `[tool.pytest.ini_options]`. Verification = `docker
  build` / `docker run` + reading runner logs.
- The user runs builds/tests/dev-servers and pastes errors back. Do not
  run `pip install`, `docker build`, `docker run`, or other long-running
  commands unless asked.
- Public surface = the re-exports in
  `src/process_tree_monitor/__init__.py`. Everything else is internal.
