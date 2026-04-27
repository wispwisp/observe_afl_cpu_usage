# afl-cpu-monitor

CPU usage monitor for AFL++ fuzzing process trees. Given the PID of an
AFL++ master process, it samples CPU usage of that process and every
descendant it spawns, emits one `Sample` per tick, and delivers each
sample to an `on_sample` callback.

## Install

```
pip install -e .
```

Requires Python 3.10+, Linux (uses `/proc` via `psutil`), and `psutil`.

## Quick start

```python
import logging
import subprocess
from afl_cpu_monitor import CpuTreeMonitor, Sample

def emit_to_otel(sample: Sample) -> None:
    # Replace with a real OpenTelemetry meter / exporter in production.
    logging.getLogger("otel").info(
        "agg=%.1f%% procs=%d", sample.aggregate_cpu_percent, sample.process_count,
    )

afl = subprocess.Popen(
    ["afl-fuzz", "-i", "in", "-o", "out", "--", "./target"],
    start_new_session=True,
)

with CpuTreeMonitor(
    root_pids=[afl.pid],
    on_sample=emit_to_otel,
    interval_s=1.0,
    top_n=10,
) as monitor:
    afl.wait()
```

`root_pids` accepts a single int or a list of ints (parallel `-M`/`-S`).

## Sample schema

`Sample.schema_version == 1`. See `src/afl_cpu_monitor/samples.py` for
the pydantic model; `Sample.model_dump()` returns a plain dict suitable
for use as OpenTelemetry attributes, and `Sample.model_dump_json()` /
`Sample.model_json_schema()` are also available.

## Demo (Docker)

```
docker build -t afl-cpu-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it -v "$PWD/out:/out" afl-cpu-monitor-demo
```

Inside the container `runner.py` launches AFL++ against a crashing
C++ target (`docker_demo/target/target.cc`), attaches the monitor, and
prints one `[otel] …` line per second standing in for a real OTel
exporter call.

## Limitations

psutil sampling cannot account for CPU consumed by child processes that
are born **and** die between two ticks. AFL++ in non-persistent fork-server
mode spawns extremely short-lived target processes, so the reported
aggregate may underestimate actual CPU. Persistent mode (AFL's
`AFL_LOOP_TIME` / `__AFL_LOOP`) avoids this. A cgroup v2 backend that
captures cumulative CPU including dead children is on the roadmap; the
sampler interface is kept clean so it slots in behind the same
`CpuTreeMonitor` API.
