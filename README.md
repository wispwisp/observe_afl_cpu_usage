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
    # cpu_percent is a gauge (cores-busy, like top); the CpuTimes second
    # fields stay counters. cpu_percent / elapsed_s are None on the first
    # tick (no baseline to diff against).
    cpu = sample.aggregate
    mem = sample.memory_aggregate
    load = "n/a" if sample.cpu_percent is None else f"{sample.cpu_percent:.0f}%"
    logging.getLogger("otel").info(
        "load=%s cpu u=%.1fs s=%.1fs | mem rss=%dK vms=%dK pss=%s | procs=%d",
        load, cpu.user_seconds, cpu.system_seconds,
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

## Recent CPU load (`cpu_percent`)

Each `Sample` carries a recent CPU-load percentage at three levels —
`Sample.cpu_percent` (whole tree), `RootSample.cpu_percent` (per root),
and `ProcSample.cpu_percent` (per process). It is **cores-busy**, like
`top`: `delta_cpu_seconds / elapsed_s × 100`, where the per-PID delta uses
`user + system + children_user + children_system`. One fully-used core
reads ~100; a multithreaded process or a tree aggregate can exceed 100.

`Sample.elapsed_s` is the **measured** wall interval used as the
denominator (distinct from the nominal `interval_s`), so any percentage is
auditable. All of these are `None` until a baseline exists: the first tick,
the first tick after a `stop()`/`start()` restart, a newly-appeared PID, or
a PID whose `create_time` no longer matches (reuse).

Because per-PID totals include `children_*`, a long-lived process's load
reflects short-lived children it reaps — for AFL the fork-server's
`cpu_percent` reflects target-execution throughput even though no
individual target is ever directly sampled. The trade-off: a child that
lives long enough to be sampled across several ticks and is then reaped
folds its accumulated CPU into the parent's `children_*` in one interval, a
one-tick spike in that interval (rare for AFL; the integral stays correct).

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
`Sample.model_json_schema()` are also available. Every sampled process is
exposed, unranked, as `Sample.processes` — a tuple of `ProcSample` in
tree-discovery order (roots first), each carrying its `cpu_percent`,
`cpu_times`, and `memory`. There is no top-N; sort `processes` yourself if
you want the heaviest by any dimension.

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

**Recent load:** `cpu_percent` and `elapsed_s` are `None` on the first
tick and immediately after a restart. The `children_*`-inclusive total can
produce a one-tick spike when a multi-tick-lived child is finally reaped
(rare for AFL).

A cgroup v2 backend that captures cumulative CPU and current/peak
memory regardless of process lifecycle is on the roadmap; the sampler
interface is kept clean so it slots in behind the same
`ProcessTreeMonitor` API.
