# cpu-process-tree-monitor — project memory for future Claude sessions

## What this project is

A Python subsystem that monitors CPU usage of an AFL++ fuzzing process
and its descendant tree. Drops into existing infrastructure that already
launches AFL++ via `subprocess`. Each tick produces one `Sample` and
delivers it to a single `on_sample` callback supplied by the caller;
the production target is OpenTelemetry.

## Frozen design choices

These were chosen deliberately and survived two simplification passes.
Do not reintroduce removed pieces without asking the user.

- **`psutil` only; no per-PID cache.** Each tick walks
  `psutil.Process(root).children(recursive=True)` and reads `cpu_times()`
  per PID. `cpu_times()` is cumulative (monotonic counters, OTel-shaped),
  so a fresh `psutil.Process` per tick is correct and simpler than caching.
- **Single `on_sample` callback.** No `Sink` protocol, no fanout, no JSONL
  writer, no watchdog tailing. Failures inside the callback are caught
  and logged. The OTel adapter lives in the caller — see
  `docker_demo/runner.py:emit_to_otel` for the stand-in.
- **Multi-root is first-class.** `root_pids: list[int]`; `Sample` carries
  per-root sub-aggregates plus a global aggregate (parallel `-M`/`-S`).
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
- **Scope is strictly CPU.** Memory, per-core breakdown, and threshold
  alerts are out of scope.

## Known limitation

The born-and-die-between-ticks gap is largely closed by `cpu_times()`'s
`children_user` / `children_system`: when the kernel reaps a child, its
CPU is added to the still-alive parent's `children_*`. AFL's master and
fork-server stay alive throughout, so target-binary CPU is captured via
the fork-server. Remaining narrow gap: an intermediate parent that exits
while a child is still alive reparents that child to init, which we
don't sample. Rare in AFL. A future cgroup v2 backend would close this.

## How to run the demo

```
docker build -t cpu-process-tree-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it cpu-process-tree-monitor-demo
```

Expect log lines from `emit_to_otel` with non-zero aggregate CPU and top
processes including `afl-fuzz` and `target`; AFL crashes within seconds.

## Conventions

- Python 3.9, type hints throughout. Linux-only on purpose (`psutil`
  reads `/proc`).
- **No automated test suite.** No `tests/` directory, no `pytest` in
  dependencies, no `[tool.pytest.ini_options]`. Verification = `docker
  build` / `docker run` + reading runner logs.
- The user runs builds/tests/dev-servers and pastes errors back. Do not
  run `pip install`, `docker build`, `docker run`, or other long-running
  commands unless asked.
- Public surface = the re-exports in
  `src/cpu_process_tree_monitor/__init__.py`. Everything else is internal.
