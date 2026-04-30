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


class MemoryInfo(_Frozen):
    rss_bytes: int
    vms_bytes: int
    shared_bytes: int
    # Populated only when full_memory_info=True AND the per-PID
    # memory_full_info() call succeeded. None on the per-PID fallback
    # path (AccessDenied, etc.).
    uss_bytes: int | None = None
    pss_bytes: int | None = None
    swap_bytes: int | None = None


class ProcSample(_Frozen):
    pid: int
    comm: str
    cpu_times: CpuTimes
    memory: MemoryInfo


class RootSample(_Frozen):
    root_pid: int
    root_alive: bool
    descendant_count: int
    aggregate: CpuTimes
    memory_aggregate: MemoryInfo
    # Count of root+descendant PIDs whose memory.pss_bytes is populated.
    # None when the monitor was configured with full_memory_info=False.
    memory_full_info_pid_count: int | None


class Sample(_Frozen):
    timestamp_unix: float
    interval_s: float
    roots: tuple[RootSample, ...]
    process_count: int
    aggregate: CpuTimes
    memory_aggregate: MemoryInfo
    memory_full_info_pid_count: int | None
    top_processes_by_cpu: tuple[ProcSample, ...]
    top_processes_by_memory: tuple[ProcSample, ...]
