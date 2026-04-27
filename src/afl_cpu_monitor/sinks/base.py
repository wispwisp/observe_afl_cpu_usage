"""Sink protocol and a fanout helper."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..samples import Sample


@runtime_checkable
class Sink(Protocol):
    def handle(self, sample: Sample) -> None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


class MultiSink:
    """Fans a Sample out to multiple sinks. Sub-sink exceptions propagate.

    For per-sink isolation, add each sink directly to the monitor instead
    of grouping them in a MultiSink — the monitor catches per-sink errors
    and disables a sink after repeated failures.
    """

    def __init__(self, *sinks: Sink) -> None:
        self._sinks: list[Sink] = list(sinks)

    def handle(self, sample: Sample) -> None:
        for s in self._sinks:
            s.handle(sample)

    def flush(self) -> None:
        for s in self._sinks:
            s.flush()

    def close(self) -> None:
        for s in self._sinks:
            s.close()
