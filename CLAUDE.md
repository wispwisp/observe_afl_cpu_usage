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

## How to run the demo

```
docker build -t process-tree-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it process-tree-monitor-demo
```

Expect log lines from `emit_to_otel` with non-zero CPU and memory
aggregates and top processes (by CPU and by memory) including
`afl-fuzz` and `target`; AFL crashes within seconds. Add
`--full-memory-info` to populate PSS/USS/swap fields.

## Conventions

- Python 3.10+, type hints throughout. Linux-only on purpose (`psutil`
  reads `/proc`).
- **No automated test suite.** No `tests/` directory, no `pytest` in
  dependencies, no `[tool.pytest.ini_options]`. Verification = `docker
  build` / `docker run` + reading runner logs.
- The user runs builds/tests/dev-servers and pastes errors back. Do not
  run `pip install`, `docker build`, `docker run`, or other long-running
  commands unless asked.
- Public surface = the re-exports in
  `src/process_tree_monitor/__init__.py`. Everything else is internal.
