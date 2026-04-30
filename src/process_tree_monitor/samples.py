"""Sample models."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class CpuTimes(_Frozen):
    user_seconds: float
    system_seconds: float
    children_user_seconds: float
    children_system_seconds: float


class ProcSample(_Frozen):
    pid: int
    comm: str
    cpu_times: CpuTimes


class RootSample(_Frozen):
    root_pid: int
    root_alive: bool
    descendant_count: int
    aggregate: CpuTimes


class Sample(_Frozen):
    timestamp_unix: float
    interval_s: float
    roots: tuple[RootSample, ...]
    process_count: int
    aggregate: CpuTimes
    top_processes: tuple[ProcSample, ...]
