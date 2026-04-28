# cpu-process-tree-monitor — project memory for future Claude sessions

## What this project is

A Python subsystem that monitors CPU usage of an AFL++ fuzzing process
and its entire descendant process tree (children, grandchildren, etc.).
Designed to drop into existing infrastructure that already launches
AFL++ via `subprocess`. Each tick the subsystem produces one `Sample`
and delivers it to a single `on_sample` callback supplied by the
caller; the production target for that callback is OpenTelemetry.

## Origin (the dialog this came from)

The user maintains production Python infrastructure that runs AFL++
fuzzing for C/C++ projects. AFL++ is started via `subprocess.run` /
`Popen`. The user asked for a new CPU-monitoring subsystem that, given
AFL's PID, tracks CPU consumption of the whole tree of processes AFL
spawns, then later asked for a simplification pass that removed
JSONL-on-disk + watchdog tailing and a `Sink` Protocol (originally the
headline integration) in favour of a single `on_sample` callback —
because the real downstream is OpenTelemetry and a callback is the
cleanest place to plug a meter / exporter in. A second simplification
pass cut the sampler/monitor down to the minimum needed to feed AFL
samples into the callback (no PID-reuse defense, no zombie/auto-stop
logic, no drift-corrected scheduling, no schema_version).

## Design choices (frozen)

- **Sampling backend**: `psutil` only. Each tick calls
  `psutil.Process(root).children(recursive=True)` per root and reuses
  the per-PID `psutil.Process` cache that the sampler already needs for
  `cpu_percent(interval=None)` deltas. There is no hand-rolled
  `/proc` walker — `psutil` does that internally.
- **No cgroup backend**. The runner script and AFL run "at the same
  level" — the script just receives a PID and walks the tree. The
  sampler interface is kept clean so a cgroup v2 backend can be added
  later without API churn.
- **Multi-root**: `root_pids: list[int]` is first-class. Each `Sample`
  carries per-root sub-aggregates (`Sample.roots[i]`) plus the global
  aggregate. Useful for parallel AFL `-M`/`-S` mode.
- **Single callback integration**: `CpuTreeMonitor` takes one
  `on_sample: Callable[[Sample], None]` and calls it under a
  `try/except` (failures are logged, not propagated). No fanout, no
  Sink protocol, no JSONL writer, no watchdog handler. The OTel
  adapter lives in the caller. See `docker_demo/runner.py` for a
  stand-in (`emit_to_otel`).
- **Scope is strictly CPU**. Memory, per-core breakdown, and threshold
  alerts are explicitly out of scope.
- **The library installs no signal handlers**. The runner owns
  SIGINT/SIGTERM and calls `monitor.stop()`.
- **Lifecycle**: a single daemon `threading.Thread` runs a private
  `schedule.Scheduler()` instance ticking the sampler every
  `interval_s`. `stop()` sets a `threading.Event` and joins the thread.
  No auto-stop on root exit — the runner watches its own subprocess
  and calls `monitor.stop()`. Restart after `stop()` is supported.
- **No PID-reuse defense and no explicit zombie check**. The cache is
  keyed by pid, and a dead or zombie root just produces a zero
  `RootSample`. Both checks were dropped in the simplification pass.

## Known limitation (documented, not a bug)

psutil sampling cannot account for CPU consumed by child processes
that are born **and** die between two ticks. AFL++ in non-persistent
fork-server mode spawns extremely short-lived target binaries, so the
reported aggregate may underestimate actual CPU. AFL persistent mode
(`__AFL_LOOP`) keeps targets alive across executions and avoids this.
A future cgroup v2 backend can close this gap because `cpu.stat`
includes CPU time of children that have already exited.

## File layout

```
src/cpu_process_tree_monitor/
  __init__.py      public re-exports
  monitor.py       CpuTreeMonitor (thread + schedule.Scheduler driver)
  sampler.py       PsutilTreeSampler (psutil-based, the only backend)
  samples.py       Sample / RootSample / ProcSample pydantic models

docker_demo/       Dockerfile + crashing C++ target + runner.py smoke test
```

There is intentionally no `tests/` directory and no `proc.py` /
`sinks/` module.

## How to run the demo

```
docker build -t cpu-process-tree-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it -v "$PWD/out:/out" cpu-process-tree-monitor-demo
```

Expect: log lines from `emit_to_otel` showing non-zero aggregate CPU
and top processes including `afl-fuzz` and `target`. AFL discovers the
`bug!` crash within seconds.

## Conventions for future edits

- Python 3.10+. Type hints throughout. No emojis.
- No comments unless WHY is non-obvious.
- The user runs builds/tests/dev-servers themselves and pastes errors
  back. Do not run `pip install`, `docker build`, `docker run`, or any
  long-running invocation yourself unless the user asks.
- **No automated test suite.** Verification happens via `docker build`
  / `docker run` and reading runner logs. Do not add a `tests/`
  directory, do not add `pytest` to dependencies, and do not
  reintroduce `[tool.pytest.ini_options]` to `pyproject.toml`.
- v1 is Linux-only on purpose (`psutil` reads `/proc`).
- Public surface = what's re-exported from
  `src/cpu_process_tree_monitor/__init__.py`. Everything else is internal.
