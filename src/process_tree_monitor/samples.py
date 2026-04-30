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


_ZERO_MEMORY = MemoryInfo(rss_bytes=0, vms_bytes=0, shared_bytes=0)


class ProcSample(_Frozen):
    pid: int
    comm: str
    cpu_times: CpuTimes
    # Interim default; sampler populates this in Task 3. Made required in Task 5.
    memory: MemoryInfo = _ZERO_MEMORY


class RootSample(_Frozen):
    root_pid: int
    root_alive: bool
    descendant_count: int
    aggregate: CpuTimes
    # Interim defaults; sampler populates these in Task 4. Made required in Task 5.
    memory_aggregate: MemoryInfo = _ZERO_MEMORY
    memory_full_info_pid_count: int | None = None


class Sample(_Frozen):
    timestamp_unix: float
    interval_s: float
    roots: tuple[RootSample, ...]
    process_count: int
    aggregate: CpuTimes
    # Old field name kept temporarily so the existing sampler still constructs
    # a valid Sample. Renamed/split in Task 5 once the sampler is updated.
    top_processes: tuple[ProcSample, ...]
    # Interim defaults; sampler populates these in Task 4–5. Made required in Task 5.
    memory_aggregate: MemoryInfo = _ZERO_MEMORY
    memory_full_info_pid_count: int | None = None
    top_processes_by_cpu: tuple[ProcSample, ...] = ()
    top_processes_by_memory: tuple[ProcSample, ...] = ()
