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
