"""Append-only JSONL file sink. One sample per line."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from ..samples import Sample


class JsonlFileSink:
    """Writes one JSON object per line to a file. Each handle() does a
    single os.write of `line + "\\n"`, so concurrent readers (e.g. a
    watchdog Observer) see complete lines.

    The file is opened with O_APPEND so concurrent writers don't
    truncate each other and crash-recovery is well-defined.
    """

    def __init__(self, path: str | os.PathLike, *, fsync: bool = False) -> None:
        self._path = Path(path)
        self._fsync = bool(fsync)
        self._fd: int | None = None
        self._lock = threading.Lock()

    def _open(self) -> int:
        if self._fd is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(
                str(self._path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o644,
            )
        return self._fd

    def handle(self, sample: Sample) -> None:
        data = (sample.to_json() + "\n").encode("utf-8")
        with self._lock:
            fd = self._open()
            os.write(fd, data)
            if self._fsync:
                os.fsync(fd)

    def flush(self) -> None:
        with self._lock:
            if self._fd is not None and self._fsync:
                os.fsync(self._fd)

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
