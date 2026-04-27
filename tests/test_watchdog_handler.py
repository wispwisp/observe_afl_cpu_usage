"""CpuSampleEventHandler: byte-offset tail correctness."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import FileModifiedEvent, FileCreatedEvent

from afl_cpu_monitor import (
    CpuSampleEventHandler,
    JsonlFileSink,
    ProcSample,
    RootSample,
    Sample,
)
from afl_cpu_monitor.samples import SCHEMA_VERSION


def _sample(pct: float) -> Sample:
    return Sample(
        schema_version=SCHEMA_VERSION,
        timestamp_unix=1.0,
        monotonic_s=1.0,
        interval_s=1.0,
        elapsed_s=1.0,
        ncpu=4,
        roots=(RootSample(root_pid=1, root_alive=True, descendant_count=0, aggregate_cpu_percent=pct),),
        process_count=1,
        aggregate_cpu_percent=pct,
        normalized_cpu_percent=pct / 4,
        top_processes=(),
        sampling_overhead_ms=0.0,
        dropped_pids=0,
    )


class CapturingHandler(CpuSampleEventHandler):
    def __init__(self, path):
        super().__init__(path)
        self.received: list[Sample] = []

    def on_cpu_sample(self, sample: Sample) -> None:
        self.received.append(sample)


def test_skips_preexisting_content_by_default(tmp_path: Path):
    path = tmp_path / "cpu.jsonl"
    sink = JsonlFileSink(path)
    sink.handle(_sample(10.0))  # pre-existing
    sink.close()
    handler = CapturingHandler(path)
    # Simulate a modification event with no new content.
    handler.on_modified(FileModifiedEvent(str(path)))
    assert handler.received == []


def test_dispatches_each_appended_line(tmp_path: Path):
    path = tmp_path / "cpu.jsonl"
    handler = CapturingHandler(path)
    sink = JsonlFileSink(path)

    sink.handle(_sample(10.0))
    handler.on_created(FileCreatedEvent(str(path)))
    handler.on_modified(FileModifiedEvent(str(path)))
    sink.handle(_sample(20.0))
    handler.on_modified(FileModifiedEvent(str(path)))
    sink.handle(_sample(30.0))
    handler.on_modified(FileModifiedEvent(str(path)))
    sink.close()

    pcts = [s.aggregate_cpu_percent for s in handler.received]
    assert pcts == [10.0, 20.0, 30.0]


def test_partial_line_buffered_until_newline(tmp_path: Path):
    path = tmp_path / "cpu.jsonl"
    handler = CapturingHandler(path)
    handler._pos = 0  # consume from start

    json_line = _sample(42.0).to_json()
    half = len(json_line) // 2

    path.write_bytes(json_line[:half].encode())
    handler.on_modified(FileModifiedEvent(str(path)))
    assert handler.received == []

    with path.open("ab") as f:
        f.write((json_line[half:] + "\n").encode())
    handler.on_modified(FileModifiedEvent(str(path)))
    assert len(handler.received) == 1
    assert handler.received[0].aggregate_cpu_percent == 42.0


def test_backfill_consumes_existing(tmp_path: Path):
    path = tmp_path / "cpu.jsonl"
    sink = JsonlFileSink(path)
    sink.handle(_sample(11.0))
    sink.handle(_sample(22.0))
    sink.close()
    handler = CapturingHandler(path)
    handler.backfill()
    pcts = [s.aggregate_cpu_percent for s in handler.received]
    assert pcts == [11.0, 22.0]


def test_unrelated_file_event_ignored(tmp_path: Path):
    path = tmp_path / "cpu.jsonl"
    other = tmp_path / "other.txt"
    other.write_text("hi\n")
    handler = CapturingHandler(path)
    handler.on_modified(FileModifiedEvent(str(other)))
    assert handler.received == []
