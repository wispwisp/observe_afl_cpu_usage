"""Sink behavior: JSONL round-trip, callback isolation, sink failure handling."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from afl_cpu_monitor import (
    CallbackSink,
    CpuTreeMonitor,
    JsonlFileSink,
    ProcSample,
    RootSample,
    Sample,
)
from afl_cpu_monitor.samples import SCHEMA_VERSION


BUSY = "import time; end=time.monotonic()+10\nwhile time.monotonic()<end: pass\n"


def _make_sample() -> Sample:
    return Sample(
        schema_version=SCHEMA_VERSION,
        timestamp_unix=1.0,
        monotonic_s=2.0,
        interval_s=1.0,
        elapsed_s=1.0,
        ncpu=8,
        roots=(RootSample(root_pid=42, root_alive=True, descendant_count=1, aggregate_cpu_percent=33.0),),
        process_count=2,
        aggregate_cpu_percent=33.0,
        normalized_cpu_percent=4.125,
        top_processes=(
            ProcSample(pid=42, ppid=1, comm="afl-fuzz", cpu_percent=20.0,
                       cpu_time_user_s=1.0, cpu_time_system_s=0.1),
            ProcSample(pid=99, ppid=42, comm="target", cpu_percent=13.0,
                       cpu_time_user_s=0.5, cpu_time_system_s=0.05),
        ),
        sampling_overhead_ms=2.5,
        dropped_pids=0,
    )


def test_sample_json_roundtrip():
    s = _make_sample()
    line = s.to_json()
    parsed = Sample.from_json(line)
    assert parsed == s


def test_jsonl_sink_writes_one_line_per_sample(tmp_path: Path):
    path = tmp_path / "out" / "cpu.jsonl"
    sink = JsonlFileSink(path)
    s1 = _make_sample()
    s2 = _make_sample()
    sink.handle(s1)
    sink.handle(s2)
    sink.close()
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        parsed = Sample.from_json(line)
        assert parsed.schema_version == 1


def test_callback_sink_invokes_with_sample():
    received: list[Sample] = []
    sink = CallbackSink(received.append)
    s = _make_sample()
    sink.handle(s)
    assert received == [s]


def test_monitor_isolates_failing_sink(tmp_path: Path):
    proc = subprocess.Popen([sys.executable, "-c", BUSY])
    received: list[Sample] = []

    class BadSink:
        def handle(self, sample): raise RuntimeError("boom")
        def flush(self): pass
        def close(self): pass

    try:
        m = CpuTreeMonitor(
            root_pids=proc.pid,
            interval_s=0.2,
            top_n=3,
            sinks=[BadSink(), CallbackSink(received.append)],
        )
        with m:
            time.sleep(1.2)
        # Good sink must keep receiving despite bad sink raising every tick.
        assert len(received) >= 3
    finally:
        proc.kill()
        proc.wait(timeout=5)
