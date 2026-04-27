"""CpuTreeMonitor — lifecycle, threading, and callback dispatch."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable

from .sampler import PsutilTreeSampler
from .samples import Sample

log = logging.getLogger(__name__)

SampleCallback = Callable[[Sample], None]


class CpuTreeMonitor:
    """Periodically samples the CPU usage of one or more process trees and
    delivers each Sample to a single callback.

    The library installs no signal handlers — the caller owns SIGINT/SIGTERM
    and is expected to call .stop() to drive shutdown.

    After .stop() the monitor may be restarted with .start() (a fresh
    psutil cache is built).
    """

    def __init__(
        self,
        root_pids: int | Iterable[int],
        on_sample: SampleCallback,
        *,
        interval_s: float = 1.0,
        top_n: int = 10,
        stop_when_all_roots_exit: bool = True,
        grace_after_exit_s: float = 2.0,
        thread_name: str = "afl-cpu-monitor",
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

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sample: Sample | None = None
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
            self._stop_event.clear()
            self._sampler.attach()
            self._thread = threading.Thread(
                target=self._run, name=self._thread_name, daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            if not self.is_running:
                self._sampler.detach()
                return
            self._stop_event.set()
            t = self._thread
        if t is not None:
            t.join(timeout=timeout)
            if t.is_alive():
                self._log.warning(
                    "monitor thread did not stop within %.1fs; "
                    "skipping detach to avoid racing the worker",
                    timeout,
                )
                return
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

    def _run(self) -> None:
        loop_start = time.monotonic()
        n = 0
        grace_start: float | None = None
        try:
            while not self._stop_event.is_set():
                tick_target = loop_start + n * self._interval_s
                wait = tick_target - time.monotonic()
                if wait > 0 and self._stop_event.wait(wait):
                    break
                n += 1

                try:
                    sample = self._sampler.sample(
                        interval_s=self._interval_s, top_n=self._top_n,
                    )
                except Exception:
                    self._log.exception("sampler raised; continuing")
                    continue

                # Cross-thread store: atomic on CPython under the GIL.
                self._last_sample = sample
                try:
                    self._on_sample(sample)
                except Exception:
                    self._log.exception("on_sample callback raised; continuing")

                lag = time.monotonic() - tick_target
                if lag > 2 * self._interval_s:
                    self._log.warning(
                        "monitor falling behind: tick %d lag=%.2fs > 2*%.2fs",
                        n, lag, self._interval_s,
                    )

                if self._stop_when_all_roots_exit:
                    any_root_alive = any(r.root_alive for r in sample.roots)
                    if not any_root_alive:
                        if grace_start is None:
                            grace_start = time.monotonic()
                            self._log.info(
                                "all roots exited; entering %.1fs grace period",
                                self._grace_after_exit_s,
                            )
                        elif time.monotonic() - grace_start >= self._grace_after_exit_s:
                            self._log.info("grace period ended; stopping monitor")
                            break
                    else:
                        grace_start = None
        except Exception:
            self._log.exception("monitor loop crashed")
