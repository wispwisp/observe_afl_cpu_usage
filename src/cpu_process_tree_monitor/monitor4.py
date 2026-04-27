"""CpuTreeMonitor backed by the `schedule` library.

Drop-in alternative to .monitor.CpuTreeMonitor: same constructor and
public methods, but the periodic loop is delegated to a private
schedule.Scheduler instance driven by a worker thread that calls
run_pending() and sleeps until the next scheduled job.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable

import schedule

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
        self._scheduler: schedule.Scheduler | None = None
        self._stop_event = threading.Event()
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
            self._stop_event.clear()
            # Private Scheduler() instance instead of the schedule module
            # singleton — sharing the global would cross-talk with any other
            # caller that also uses `schedule`.
            sched = schedule.Scheduler()
            sched.every(self._interval_s).seconds.do(self._tick)
            self._scheduler = sched
            self._thread = threading.Thread(
                target=self._run_loop,
                name=self._thread_name,
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            t = self._thread
            if t is None or not t.is_alive():
                self._thread = None
                self._scheduler = None
                self._sampler.detach()
                return

        self._stop_event.set()
        t.join(timeout=timeout)
        if t.is_alive():
            self._log.warning(
                "schedule worker did not stop within %.1fs; "
                "skipping detach to avoid racing the worker",
                timeout,
            )
            return

        with self._lock:
            self._thread = None
            self._scheduler = None
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
        sched = self._scheduler
        if sched is None:
            return
        try:
            while not self._stop_event.is_set():
                sched.run_pending()
                idle = sched.idle_seconds
                if idle is None or idle < 0:
                    idle = self._interval_s
                self._stop_event.wait(timeout=min(idle, self._interval_s))
        finally:
            sched.clear()

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
                    self._stop_event.set()
            else:
                self._grace_start = None
