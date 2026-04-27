# afl-cpu-monitor — project memory for future Claude sessions

## What this project is

A Python subsystem that monitors CPU usage of an AFL++ fuzzing process and
its entire descendant process tree (children, grandchildren, etc.).
Designed to drop into existing infrastructure that already launches
AFL++ via `subprocess` and observes events via the `watchdog` library.

## Origin (the dialog this came from)

The user maintains production Python infrastructure that runs AFL++
fuzzing for C/C++ projects. AFL++ is started via `subprocess.run`/`Popen`,
and an existing observation system built on the `watchdog` filesystem
events library handles other event streams (crashes, queue churn, etc.).
They asked for a new CPU-monitoring subsystem that, given AFL's PID,
tracks CPU consumption of the whole tree of processes AFL spawns.

## Design choices (frozen in v1)

- **Sampling backend**: `psutil` + `/proc` only. One pass over `/proc`
  per tick builds a `pid -> ppid` map; per-root BFS yields each tree.
  See `src/afl_cpu_monitor/proc.py` and `src/afl_cpu_monitor/sampler.py`.
- **No cgroup backend in v1**. The user clarified that the runner
  script and AFL run "at the same level" — the script just receives a
  PID and walks the tree. The Docker image is a *test harness*, not a
  privileged cgroup host. The `Sampler`-shaped surface is kept clean so
  a cgroup v2 backend can be added later without API churn.
- **Multi-root**: `root_pids: list[int]` is first-class. Each `Sample`
  carries per-root sub-aggregates (`Sample.roots[i]`) plus the global
  aggregate. The user requested this for parallel AFL `-M`/`-S` mode.
- **Watchdog integration**: the headline integration is a subclass of
  `watchdog.events.FileSystemEventHandler`
  (`afl_cpu_monitor.CpuSampleEventHandler`) that tails the JSONL file
  written by `JsonlFileSink`. The user's existing `watchdog.Observer`
  schedules this handler on the directory containing `cpu.jsonl`, and
  `on_cpu_sample(sample: Sample)` is the override point. The user
  explicitly chose this pattern over a parallel observer.
- **In-process integration also supported**: `CallbackSink(fn)` for
  callers that don't want the file roundtrip. `LoggingSink`,
  `MultiSink` round out the built-in set.
- **Scope is strictly CPU**. The user explicitly excluded memory,
  per-core breakdown, and threshold alerts from v1.
- **The library installs no signal handlers**. The runner owns
  SIGINT/SIGTERM and calls `monitor.stop()`. See
  `docker_demo/runner.py` for the pattern.
- **Lifecycle**: a single daemon `threading.Thread` driven by a
  `threading.Event` stop flag. Drift-corrected scheduling against
  `time.monotonic()`. Stops automatically when *all* roots exit
  (configurable via `stop_when_all_roots_exit`, with
  `grace_after_exit_s` tail). Restart after `stop()` is supported.
- **Sink failure isolation**: per-sink `try/except` in the dispatcher;
  three consecutive failures auto-disable a sink with one ERROR log.
- **PID reuse defense**: the psutil cache is keyed by pid, but
  `create_time()` is verified on every cache hit; mismatched
  create-times trigger eviction and a fresh psutil.Process instance.

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
  __init__.py              public re-exports
  monitor.py               CpuTreeMonitor (lifecycle, thread, sink fanout)
  sampler.py               PsutilTreeSampler (the only backend in v1)
  samples.py               Sample / RootSample / ProcSample dataclasses + JSON
  proc.py                  /proc helpers (pid->ppid map, BFS, num_cpus)
  sinks/
    base.py                Sink protocol + MultiSink fanout
    callback.py            CallbackSink
    jsonl.py               JsonlFileSink (single-writer, O_APPEND, atomic line)
    logging.py             LoggingSink
    watchdog_handler.py    CpuSampleEventHandler (watchdog FileSystemEventHandler)

tests/                     pytest suite — sampler, lifecycle, sinks, watchdog handler
docker_demo/               Dockerfile + crashing C++ target + runner.py smoke test
```

## How to run the demo

```
docker build -t afl-cpu-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it -v "$PWD/out:/out" afl-cpu-monitor-demo
```

Expect: `out/cpu.jsonl` populated with one JSON sample per second; the
runner's `DemoHandler.on_cpu_sample` log lines show non-zero aggregate
CPU and top processes including `afl-fuzz` and `target`. AFL discovers
the `bug!` crash within seconds.

## Conventions for future edits

- Python 3.10+. Type hints throughout. No emojis.
- No comments unless WHY is non-obvious.
- The user runs builds/tests/dev-servers themselves and pastes errors
  back. Do not run `pytest`, `docker build`, `docker run`, or any
  long-running invocation yourself unless the user asks.
- v1 is Linux-only on purpose (`/proc`, `os.sched_getaffinity`).
- `Sample.schema_version` must be bumped if the JSON shape changes;
  `from_json` should remain backward-compatible across at least one
  version.
- Public surface = what's re-exported from
  `src/afl_cpu_monitor/__init__.py`. Everything else is internal.
