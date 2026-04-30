"""ProcessTreeMonitor: a daemon thread that ticks a PsutilTreeSampler via
schedule.Scheduler and dispatches each Sample to a single callback.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable

import schedule

from .sampler import PsutilTreeSampler
from .samples import Sample

log = logging.getLogger(__name__)

SampleCallback = Callable[[Sample], None]


class ProcessTreeMonitor:
    def __init__(
        self,
        root_pids: int | Iterable[int],
        on_sample: SampleCallback,
        *,
        interval_s: float = 1.0,
        top_n: int = 10,
        full_memory_info: bool = False,
    ) -> None:
        roots = [root_pids] if isinstance(root_pids, int) else list(root_pids)
        self._sampler = PsutilTreeSampler(roots, full_memory_info=full_memory_info)
        self._on_sample = on_sample
        self._interval_s = max(0.1, float(interval_s))
        self._top_n = int(top_n)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        sched = schedule.Scheduler()
        sched.every(self._interval_s).seconds.do(self._tick)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(sched,),
            name="process-tree-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        self._thread = None

    def __enter__(self) -> "ProcessTreeMonitor":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _run_loop(self, sched: schedule.Scheduler) -> None:
        try:
            while not self._stop_event.is_set():
                sched.run_pending()
                self._stop_event.wait(timeout=self._interval_s)
        finally:
            sched.clear()

    def _tick(self) -> None:
        try:
            sample = self._sampler.sample(
                interval_s=self._interval_s, top_n=self._top_n,
            )
            self._on_sample(sample)
        except Exception:
            log.exception("sampler/callback error; continuing")
