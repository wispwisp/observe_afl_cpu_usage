"""Sample models."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ProcSample(_Frozen):
    pid: int
    comm: str
    cpu_percent: float


class RootSample(_Frozen):
    root_pid: int
    root_alive: bool
    descendant_count: int
    aggregate_cpu_percent: float


class Sample(_Frozen):
    timestamp_unix: float
    interval_s: float
    ncpu: int
    roots: tuple[RootSample, ...]
    process_count: int
    aggregate_cpu_percent: float
    normalized_cpu_percent: float
    top_processes: tuple[ProcSample, ...]
