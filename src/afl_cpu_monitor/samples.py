"""Sample models."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = 1


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ProcSample(_Frozen):
    pid: int
    ppid: int
    comm: str
    cpu_percent: float
    cpu_time_user_s: float
    cpu_time_system_s: float


class RootSample(_Frozen):
    root_pid: int
    root_alive: bool
    descendant_count: int
    aggregate_cpu_percent: float


class Sample(_Frozen):
    schema_version: int
    timestamp_unix: float
    monotonic_s: float
    interval_s: float
    elapsed_s: float
    ncpu: int
    roots: tuple[RootSample, ...]
    process_count: int
    aggregate_cpu_percent: float
    normalized_cpu_percent: float
    top_processes: tuple[ProcSample, ...]
    sampling_overhead_ms: float
    dropped_pids: int
    notes: tuple[str, ...] = ()
