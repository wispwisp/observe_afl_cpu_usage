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
    agg = sample.aggregate
    logging.getLogger("otel").info(
        "u=%.1fs s=%.1fs cu=%.1fs cs=%.1fs procs=%d",
        agg.user_seconds, agg.system_seconds,
        agg.children_user_seconds, agg.children_system_seconds,
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
) as monitor:
    afl.wait()
```

`root_pids` accepts a single int or a list of ints (parallel `-M`/`-S`).

## Sample schema

See `src/process_tree_monitor/samples.py` for the pydantic model;
`Sample.model_dump()` returns a plain dict suitable for use as
OpenTelemetry attributes, and `Sample.model_dump_json()` /
`Sample.model_json_schema()` are also available.

## Demo (Docker)

```
docker build -t process-tree-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it process-tree-monitor-demo
```

Inside the container `runner.py` launches AFL++ against a crashing
C++ target (`docker_demo/target/target.cc`), attaches the monitor, and
prints one `[otel] …` line per second standing in for a real OTel
exporter call.

## Limitations

The born-and-die-between-ticks gap is largely closed by reading
`cpu_times()`'s `children_user` / `children_system` on a still-alive
parent: when the kernel reaps a child, its CPU is added to the parent's
`children_*`. AFL's master and fork-server stay alive throughout a run,
so target-binary CPU is captured via the fork-server even though no
individual target is ever directly sampled.

Remaining narrow gap: if an intermediate parent exits while one of its
children is still alive, that child is reparented to init and its CPU
goes to init's `children_*` (which we don't sample). A cgroup v2 backend
that captures cumulative CPU regardless of process lifecycle is on the
roadmap; the sampler interface is kept clean so it slots in behind the
same `ProcessTreeMonitor` API.
