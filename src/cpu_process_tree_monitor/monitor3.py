"""CpuTreeMonitor backed by tornado.ioloop.PeriodicCallback.

Drop-in alternative to .monitor.CpuTreeMonitor: same constructor and
public methods, but the periodic loop is delegated to a Tornado
PeriodicCallback running on an IOLoop in a worker thread instead of a
hand-rolled threading.Event loop.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import threading
import time
from typing import Callable, Iterable

from tornado.ioloop import IOLoop, PeriodicCallback

from .sampler import PsutilTreeSampler
from .samples import Sample

log = logging.getLogger(__name__)

SampleCallback = Callable[[Sample], None]


class CpuTreeMonitor:
    def __init__(
        self,
        root_pids: int | Iterable[int],
        on_sample: SampleCallback,
        *,
        interval_s: float = 1.0,
        top_n: int = 10,
        stop_when_all_roots_exit: bool = True,
        grace_after_exit_s: float = 2.0,
        thread_name: str = "cpu-process-tree-monitor",
        logger: logging.Logger | None = None,
    ) -> None:
        if isinstance(root_pids, int):
            roots: Iterable[int] = [root_pids]
        else:
            roots = root_pids
        self._sampler = PsutilTreeSampler(roots)
        self._on_sample = on_sample
        self._interval_s = max(0.1, float(interval_s))
        self._top_n = int(top_n)
        self._stop_when_all_roots_exit = bool(stop_when_all_roots_exit)
        self._grace_after_exit_s = max(0.0, float(grace_after_exit_s))
        self._thread_name = thread_name
        self._log = logger or log

        self._thread: threading.Thread | None = None
        self._ioloop: IOLoop | None = None
        self._pcb: PeriodicCallback | None = None
        self._ready = threading.Event()
        self._last_sample: Sample | None = None
        self._grace_start: float | None = None
        self._loop_start: float = 0.0
        self._tick_n: int = 0
        self._lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            t = self._thread
        return t is not None and t.is_alive()

    @property
    def last_sample(self) -> Sample | None:
        return self._last_sample

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._sampler.attach()
            self._grace_start = None
            self._tick_n = 0
            self._loop_start = time.monotonic()
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name=self._thread_name,
                daemon=True,
            )
            self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            t = self._thread
            loop = self._ioloop
            if t is None or not t.is_alive():
                self._thread = None
                self._sampler.detach()
                return

        if loop is not None:
            pcb = self._pcb

            def _shutdown() -> None:
                if pcb is not None:
                    pcb.stop()
                IOLoop.current().stop()

            loop.add_callback(_shutdown)

        t.join(timeout=timeout)
        if t.is_alive():
            self._log.warning(
                "ioloop thread did not stop within %.1fs; "
                "skipping detach to avoid racing the worker",
                timeout,
            )
            return

        with self._lock:
            self._thread = None
        self._sampler.detach()

    def join(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    def __enter__(self) -> "CpuTreeMonitor":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _run_loop(self) -> None:
        # Tornado 6 is asyncio-backed; IOLoop.current() needs an asyncio
        # loop bound to this (non-main) thread before it will instantiate.
        asyncio.set_event_loop(asyncio.new_event_loop())
        ioloop = IOLoop.current()
        pcb = PeriodicCallback(
            self._tick,
            callback_time=datetime.timedelta(seconds=self._interval_s),
        )
        self._ioloop = ioloop
        self._pcb = pcb
        pcb.start()
        self._ready.set()
        try:
            ioloop.start()
        finally:
            pcb.stop()
            ioloop.close()
            self._ioloop = None
            self._pcb = None

    def _tick(self) -> None:
        self._tick_n += 1
        tick_target = self._loop_start + self._tick_n * self._interval_s

        try:
            sample = self._sampler.sample(
                interval_s=self._interval_s, top_n=self._top_n,
            )
        except Exception:
            self._log.exception("sampler raised; continuing")
            return

        self._last_sample = sample
        try:
            self._on_sample(sample)
        except Exception:
            self._log.exception("on_sample callback raised; continuing")

        lag = time.monotonic() - tick_target
        if lag > 2 * self._interval_s:
            self._log.warning(
                "monitor falling behind: tick %d lag=%.2fs > 2*%.2fs",
                self._tick_n, lag, self._interval_s,
            )

        if self._stop_when_all_roots_exit:
            any_root_alive = any(r.root_alive for r in sample.roots)
            if not any_root_alive:
                if self._grace_start is None:
                    self._grace_start = time.monotonic()
                    self._log.info(
                        "all roots exited; entering %.1fs grace period",
                        self._grace_after_exit_s,
                    )
                elif time.monotonic() - self._grace_start >= self._grace_after_exit_s:
                    self._log.info("grace period ended; stopping monitor")
                    pcb = self._pcb
                    loop = self._ioloop
                    if pcb is not None:
                        pcb.stop()
                    if loop is not None:
                        loop.add_callback(loop.stop)
            else:
                self._grace_start = None
