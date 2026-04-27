"""CpuTreeMonitor — lifecycle, threading, and sink fanout."""
from __future__ import annotations

import logging
import threading
import time
from typing import Iterable

from .sampler import PsutilTreeSampler
from .samples import Sample
from .sinks.base import Sink

log = logging.getLogger(__name__)

_DEFAULT_LOGGER = log
_MAX_SINK_FAILURES = 3


class CpuTreeMonitor:
    """Periodically samples the CPU usage of one or more process trees and
    fans each Sample out to registered sinks.

    The monitor never installs signal handlers — the calling runner owns
    SIGINT/SIGTERM and is expected to call .stop() to drive shutdown.

    After .stop() the monitor may be restarted with .start() (a fresh
    psutil cache is built).
    """

    def __init__(
        self,
        root_pids: int | Iterable[int],
        *,
        sinks: Iterable[Sink] = (),
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
        self._sinks: list[Sink] = list(sinks)
        self._sink_failures: dict[int, int] = {}
        self._disabled_sinks: set[int] = set()
        # psutil.cpu_percent is documented to be unreliable below ~0.1s.
        self._interval_s = max(0.1, float(interval_s))
        self._top_n = int(top_n)
        self._stop_when_all_roots_exit = bool(stop_when_all_roots_exit)
        self._grace_after_exit_s = max(0.0, float(grace_after_exit_s))
        self._thread_name = thread_name
        self._log = logger or _DEFAULT_LOGGER

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sample: Sample | None = None
        self._lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    @property
    def last_sample(self) -> Sample | None:
        return self._last_sample

    def add_sink(self, sink: Sink) -> None:
        with self._lock:
            self._sinks.append(sink)

    def remove_sink(self, sink: Sink) -> None:
        with self._lock:
            try:
                self._sinks.remove(sink)
            except ValueError:
                pass
            self._disabled_sinks.discard(id(sink))
            self._sink_failures.pop(id(sink), None)

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
                self._cleanup_sinks_and_sampler()
                return
            self._stop_event.set()
            t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        self._cleanup_sinks_and_sampler()

    def join(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    def __enter__(self) -> "CpuTreeMonitor":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _cleanup_sinks_and_sampler(self) -> None:
        with self._lock:
            for sink in self._sinks:
                try:
                    sink.flush()
                except Exception:
                    self._log.exception("sink %r flush failed", type(sink).__name__)
                try:
                    sink.close()
                except Exception:
                    self._log.exception("sink %r close failed", type(sink).__name__)
            self._sampler.detach()

    def _dispatch(self, sample: Sample) -> None:
        with self._lock:
            sinks = list(self._sinks)
            disabled = set(self._disabled_sinks)
        for sink in sinks:
            sid = id(sink)
            if sid in disabled:
                continue
            try:
                sink.handle(sample)
                self._sink_failures[sid] = 0
            except Exception:
                self._log.exception(
                    "sink %r raised; isolating", type(sink).__name__,
                )
                fails = self._sink_failures.get(sid, 0) + 1
                self._sink_failures[sid] = fails
                if fails >= _MAX_SINK_FAILURES:
                    self._log.error(
                        "sink %r disabled after %d consecutive failures",
                        type(sink).__name__, fails,
                    )
                    with self._lock:
                        self._disabled_sinks.add(sid)

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

                self._last_sample = sample
                self._dispatch(sample)

                lag = time.monotonic() - tick_target
                if lag > 2 * self._interval_s:
                    self._log.warning(
                        "monitor falling behind: tick %d lag=%.2fs > 2*%.2fs",
                        n, lag, self._interval_s,
                    )

                any_root_alive = any(r.root_alive for r in sample.roots)
                if self._stop_when_all_roots_exit and not any_root_alive:
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
