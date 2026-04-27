# afl-cpu-monitor — project memory for future Claude sessions

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
cleanest place to plug a meter / exporter in.

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
- **Lifecycle**: a single daemon `threading.Thread` driven by a
  `threading.Event` stop flag. Drift-corrected scheduling against
  `time.monotonic()`. Stops automatically when *all* roots exit
  (configurable via `stop_when_all_roots_exit`, with
  `grace_after_exit_s` tail). Restart after `stop()` is supported.
- **PID reuse defense**: psutil cache keyed by pid, but `create_time()`
  is verified on every cache hit; mismatched create-times trigger
  eviction and a fresh `psutil.Process` instance.
- **Zombie handling**: a root whose `proc.status() == STATUS_ZOMBIE` is
  treated as not-alive, so its tree contributes nothing to the
  aggregate and the auto-stop logic can fire.

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
src/afl_cpu_monitor/
  __init__.py      public re-exports
  monitor.py       CpuTreeMonitor (lifecycle, thread, callback dispatch)
  sampler.py       PsutilTreeSampler (psutil-based, the only backend)
  samples.py       Sample / RootSample / ProcSample dataclasses + to_dict

docker_demo/       Dockerfile + crashing C++ target + runner.py smoke test
```

There is intentionally no `tests/` directory and no `proc.py` /
`sinks/` module.

## How to run the demo

```
docker build -t afl-cpu-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it -v "$PWD/out:/out" afl-cpu-monitor-demo
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
- v1 is Linux-only on purpose (`/proc`, `os.sched_getaffinity`).
- `Sample.schema_version` exists so the OTel adapter can branch on
  schema if/when fields are added; bump it if the dataclass shape
  changes.
- Public surface = what's re-exported from
  `src/afl_cpu_monitor/__init__.py`. Everything else is internal.
