"""Spawn a controlled child tree and check we can see CPU."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from afl_cpu_monitor.sampler import PsutilTreeSampler


BUSY_PYTHON = (
    "import time, os; "
    "end = time.monotonic() + 5.0; "
    "x = 0\n"
    "while time.monotonic() < end:\n"
    "    x = (x + 1) ^ os.getpid()\n"
)


def _spawn_busy_root() -> subprocess.Popen:
    """Root spawns a child that does the busy loop, so the tree has depth >= 1."""
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', %r])\n"
        "child.wait()\n" % BUSY_PYTHON
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_sampler_sees_descendant_cpu():
    proc = _spawn_busy_root()
    try:
        sampler = PsutilTreeSampler([proc.pid])
        sampler.attach()
        # First sample primes; second sample has real deltas.
        time.sleep(0.5)
        _ = sampler.sample(interval_s=0.5, top_n=10)
        time.sleep(0.5)
        s = sampler.sample(interval_s=0.5, top_n=10)
        assert s.process_count >= 2
        assert any(r.root_alive for r in s.roots)
        assert s.roots[0].descendant_count >= 1
        assert s.aggregate_cpu_percent > 5.0, s
        sampler.detach()
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def test_sampler_handles_dead_root():
    proc = _spawn_busy_root()
    pid = proc.pid
    proc.kill()
    proc.wait(timeout=5)
    sampler = PsutilTreeSampler([pid])
    sampler.attach()
    time.sleep(0.2)
    s = sampler.sample(interval_s=0.2, top_n=10)
    assert s.roots[0].root_alive is False
    assert s.roots[0].descendant_count == 0
    assert s.roots[0].aggregate_cpu_percent == 0.0


def test_sampler_rejects_empty_roots():
    with pytest.raises(ValueError):
        PsutilTreeSampler([])


def test_sampler_dedupes_roots():
    s = PsutilTreeSampler([os.getpid(), os.getpid()])
    assert s.root_pids == [os.getpid()]
