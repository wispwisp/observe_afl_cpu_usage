"""CpuTreeMonitor backed by APScheduler.

Drop-in alternative to .monitor.CpuTreeMonitor: same constructor and
public methods, but the periodic loop is delegated to
apscheduler.schedulers.background.BackgroundScheduler with an
IntervalTrigger instead of a hand-rolled threading.Event loop.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

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

        self._scheduler: BackgroundScheduler | None = None
        self._last_sample: Sample | None = None
        self._grace_start: float | None = None
        self._loop_start: float = 0.0
        self._tick_n: int = 0
        self._lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            s = self._scheduler
        return s is not None and s.running

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
            self._scheduler = BackgroundScheduler(
                timezone="UTC",
                executors={
                    "default": {"type": "threadpool", "max_workers": 1},
                },
                job_defaults={
                    "coalesce": True,
                    "max_instances": 1,
                    "misfire_grace_time": None,
                },
            )
            self._scheduler.add_job(
                self._tick,
                trigger=IntervalTrigger(seconds=self._interval_s),
                id="cpu-tree-tick",
            )
            self._scheduler.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            sched = self._scheduler
            running = sched is not None and sched.running
            if not running:
                self._scheduler = None
                self._sampler.detach()
                return

        # APScheduler's shutdown(wait=True) has no timeout, so run it on a
        # helper thread and bound the wait ourselves.
        done = threading.Event()

        def _shutdown() -> None:
            try:
                sched.shutdown(wait=True)
            finally:
                done.set()

        helper = threading.Thread(
            target=_shutdown,
            name=f"{self._thread_name}-shutdown",
            daemon=True,
        )
        helper.start()
        if not done.wait(timeout=timeout):
            self._log.warning(
                "scheduler did not stop within %.1fs; "
                "skipping detach to avoid racing the worker",
                timeout,
            )
            return

        with self._lock:
            self._scheduler = None
        self._sampler.detach()

    def join(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.is_running:
            if deadline is not None and time.monotonic() >= deadline:
                return
            time.sleep(0.05)

    def __enter__(self) -> "CpuTreeMonitor":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

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
                    sched = self._scheduler
                    if sched is not None:
                        # Calling shutdown() from inside a job thread can
                        # deadlock with wait=True; defer to a helper thread.
                        threading.Thread(
                            target=lambda: sched.shutdown(wait=False),
                            name=f"{self._thread_name}-autoshutdown",
                            daemon=True,
                        ).start()
            else:
                self._grace_start = None
