# afl-cpu-monitor

CPU usage monitor for AFL++ fuzzing process trees. Given the PID of an
AFL++ master process, it samples CPU usage of that process and every
descendant it spawns, emits one `Sample` per tick, and fans samples out
to pluggable sinks.

## Install

```
pip install -e .
```

Requires Python 3.10+, Linux (uses `/proc`), and `psutil` + `watchdog`.

## Quick start

```python
import logging
import subprocess
from afl_cpu_monitor import CpuTreeMonitor, JsonlFileSink, LoggingSink

afl = subprocess.Popen(
    ["afl-fuzz", "-i", "in", "-o", "out", "--", "./target"],
    start_new_session=True,
)

with CpuTreeMonitor(
    root_pids=[afl.pid],
    sinks=[
        JsonlFileSink("cpu.jsonl"),
        LoggingSink(logging.getLogger("cpu")),
    ],
    interval_s=1.0,
    top_n=10,
) as monitor:
    afl.wait()
```

`root_pids` accepts a single int or a list of ints (parallel `-M`/`-S`).

## Watchdog integration (the headline)

If you already run a `watchdog.observers.Observer` over your output
directory, register a `CpuSampleEventHandler` subclass on it and override
`on_cpu_sample`:

```python
from watchdog.observers import Observer
from afl_cpu_monitor import CpuSampleEventHandler, Sample

class MyHandler(CpuSampleEventHandler):
    def on_cpu_sample(self, sample: Sample) -> None:
        my_event_bus.publish("cpu.sample", sample.to_dict())

observer = Observer()
observer.schedule(MyHandler("out/cpu.jsonl"), "out", recursive=False)
observer.start()
```

The handler tails the JSONL file by byte offset, so you never re-process
a line and partial mid-flush writes are buffered until newline.

## Sample schema

`Sample.schema_version == 1`. See `src/afl_cpu_monitor/samples.py` for
the dataclass; `to_json()` / `from_json()` round-trip JSONL lines.

## Demo (Docker)

```
docker build -t afl-cpu-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it -v "$PWD/out:/out" afl-cpu-monitor-demo
```

Inside the container `runner.py` launches AFL++ against a crashing
C++ target (`docker_demo/target/target.cc`), attaches the monitor,
and writes samples to `/out/cpu.jsonl` while the watchdog handler
prints them in real time.

## Limitations

psutil sampling cannot account for CPU consumed by child processes that
are born **and** die between two ticks. AFL++ in non-persistent fork-server
mode spawns extremely short-lived target processes, so the reported
aggregate may underestimate actual CPU. Persistent mode (AFL's
`AFL_LOOP_TIME` / `__AFL_LOOP`) avoids this. A cgroup v2 backend that
captures cumulative CPU including dead children is on the roadmap; the
`Sampler` interface is kept clean so it slots in behind the same
`CpuTreeMonitor` API.
