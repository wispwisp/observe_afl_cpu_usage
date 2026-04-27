"""watchdog.FileSystemEventHandler subclass that tails a JSONL sample file.

This is the headline integration point: register an instance on the
caller's existing watchdog.Observer and override on_cpu_sample().
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler

from ..samples import Sample

log = logging.getLogger(__name__)


class CpuSampleEventHandler(FileSystemEventHandler):
    """Tails a JSONL file (typically produced by JsonlFileSink) and
    dispatches each newly-appended sample to on_cpu_sample().

    Subclass and override on_cpu_sample to feed your event bus or any
    other downstream system.

    Tracks consumed bytes by offset, so re-entrancy from many quick
    modifications won't replay lines. Partial trailing lines (mid-flush)
    are buffered until the newline arrives.

    Construction defaults to skipping any pre-existing content in the
    file — only samples produced from now on will be dispatched. Call
    backfill() before starting the Observer to consume historical lines.

    Usage:

        observer = Observer()
        observer.schedule(MyHandler(jsonl_path), str(jsonl_path.parent))
        observer.start()
    """

    def __init__(self, jsonl_path: str | os.PathLike) -> None:
        super().__init__()
        self._path = Path(jsonl_path).resolve()
        self._pos = 0
        self._buf = ""
        self._lock = threading.Lock()
        if self._path.exists():
            try:
                self._pos = self._path.stat().st_size
            except OSError:
                self._pos = 0

    @property
    def path(self) -> Path:
        return self._path

    def backfill(self) -> None:
        """Consume any existing content from byte 0. Call before starting
        the Observer if you want historical samples too."""
        with self._lock:
            self._pos = 0
            self._buf = ""
            self._drain_locked()

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._same_path(event.src_path):
            with self._lock:
                self._pos = 0
                self._buf = ""
                self._drain_locked()

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._same_path(event.src_path):
            with self._lock:
                self._drain_locked()

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # If our file was renamed/replaced (e.g. log rotation), reset.
        dest = getattr(event, "dest_path", "")
        if self._same_path(event.src_path) or self._same_path(dest):
            with self._lock:
                self._pos = 0
                self._buf = ""
                self._drain_locked()

    def _same_path(self, p: str) -> bool:
        if not p:
            return False
        try:
            return Path(p).resolve() == self._path
        except OSError:
            return False

    def _drain_locked(self) -> None:
        try:
            with self._path.open("rb") as f:
                f.seek(self._pos)
                chunk = f.read()
        except FileNotFoundError:
            return
        except OSError:
            log.exception("failed reading %s", self._path)
            return
        if not chunk:
            return
        self._pos += len(chunk)
        self._buf += chunk.decode("utf-8", errors="replace")
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line = self._buf[:nl].strip()
            self._buf = self._buf[nl + 1 :]
            if not line:
                continue
            try:
                sample = Sample.from_json(line)
            except Exception:
                log.exception("failed to parse sample line: %r", line[:200])
                continue
            try:
                self.on_cpu_sample(sample)
            except Exception:
                log.exception("on_cpu_sample raised")

    def on_cpu_sample(self, sample: Sample) -> None:
        """Override in subclass."""
        return None
