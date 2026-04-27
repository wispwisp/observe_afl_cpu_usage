"""Lifecycle: start/stop idempotency, all-roots-exit shutdown, restart."""
from __future__ import annotations

import os
import subprocess
import sys
import time

from afl_cpu_monitor import CpuTreeMonitor, CallbackSink, Sample


BUSY = "import time; end=time.monotonic()+10\nwhile time.monotonic()<end: pass\n"


def _spawn() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", BUSY])


def test_start_stop_idempotent():
    proc = _spawn()
    try:
        m = CpuTreeMonitor(root_pids=proc.pid, interval_s=0.2, top_n=3)
        m.start()
        m.start()  # no-op
        time.sleep(0.5)
        m.stop()
        m.stop()  # no-op
        assert not m.is_running
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_collects_samples():
    proc = _spawn()
    received: list[Sample] = []
    try:
        m = CpuTreeMonitor(
            root_pids=[proc.pid],
            interval_s=0.2,
            top_n=3,
            sinks=[CallbackSink(received.append)],
        )
        with m:
            time.sleep(1.0)
        assert len(received) >= 2
        assert m.last_sample is not None
        assert all(s.schema_version == 1 for s in received)
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_auto_stop_when_root_exits():
    proc = _spawn()
    m = CpuTreeMonitor(
        root_pids=proc.pid,
        interval_s=0.2,
        top_n=3,
        stop_when_all_roots_exit=True,
        grace_after_exit_s=0.3,
    )
    m.start()
    time.sleep(0.4)
    proc.kill()
    proc.wait(timeout=5)
    # Wait for monitor to notice + grace period to elapse
    deadline = time.monotonic() + 5.0
    while m.is_running and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not m.is_running
    m.stop()


def test_can_restart_after_stop():
    proc = _spawn()
    try:
        m = CpuTreeMonitor(root_pids=proc.pid, interval_s=0.2, top_n=3)
        m.start()
        time.sleep(0.3)
        m.stop()
        m.start()
        time.sleep(0.3)
        assert m.last_sample is not None
        m.stop()
    finally:
        proc.kill()
        proc.wait(timeout=5)
