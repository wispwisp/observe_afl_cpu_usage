"""Logging sink — emits a one-line summary per sample."""
from __future__ import annotations

import logging

from ..samples import Sample


class LoggingSink:
    def __init__(
        self,
        logger: logging.Logger,
        level: int = logging.INFO,
        *,
        top_k_in_message: int = 3,
    ) -> None:
        self._log = logger
        self._level = level
        self._top_k = max(0, int(top_k_in_message))

    def handle(self, sample: Sample) -> None:
        top = ", ".join(
            f"{p.comm}({p.pid})={p.cpu_percent:.1f}%"
            for p in sample.top_processes[: self._top_k]
        )
        self._log.log(
            self._level,
            "cpu agg=%.1f%% norm=%.1f%% procs=%d roots_alive=%d/%d top=[%s]",
            sample.aggregate_cpu_percent,
            sample.normalized_cpu_percent,
            sample.process_count,
            sum(1 for r in sample.roots if r.root_alive),
            len(sample.roots),
            top,
        )

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass
