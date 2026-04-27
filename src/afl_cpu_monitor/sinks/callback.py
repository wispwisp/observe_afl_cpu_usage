"""In-process callback sink — the simplest integration adapter."""
from __future__ import annotations

from typing import Callable

from ..samples import Sample


class CallbackSink:
    def __init__(self, fn: Callable[[Sample], None]) -> None:
        self._fn = fn

    def handle(self, sample: Sample) -> None:
        self._fn(sample)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass
