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
    # Recent cores-busy CPU load for this PID over the last tick interval.
    # None until a baseline exists (first tick, restart, newly-appeared
    # PID, create_time mismatch). Filled in by the sampler via model_copy.
    cpu_percent: float | None = None


class RootSample(_Frozen):
    root_pid: int
    root_alive: bool
    descendant_count: int
    aggregate: CpuTimes
    memory_aggregate: MemoryInfo
    # Count of root+descendant PIDs whose memory.pss_bytes is populated.
    # None when the monitor was configured with full_memory_info=False.
    memory_full_info_pid_count: int | None
    # Sum of this root's contributing PIDs' cpu_percent values that have a
    # baseline. None on the first tick / after restart, or when none of this
    # root's PIDs has a baseline yet. Partial-coverage ticks (some PIDs
    # present last tick, some newly appeared) sum over the contributing
    # subset.
    cpu_percent: float | None


class Sample(_Frozen):
    timestamp_unix: float
    interval_s: float
    # Measured wall seconds since the previous tick — the denominator
    # actually used for cpu_percent. None on the first tick / after restart.
    # Distinct from interval_s, which is the nominal configured interval.
    elapsed_s: float | None
    roots: tuple[RootSample, ...]
    process_count: int
    aggregate: CpuTimes
    memory_aggregate: MemoryInfo
    memory_full_info_pid_count: int | None
    # Global recent cores-busy CPU load (sum of per-PID cpu_percent). Can
    # exceed 100. None on the first tick / after restart, or when no PID has
    # a baseline yet. Partial-coverage ticks (some PIDs newly appeared) sum
    # over the contributing subset.
    cpu_percent: float | None
    # Every sampled process, unranked, in tree-discovery order (each root
    # immediately followed by its descendants). Replaces the former ranked
    # top-N tuples.
    processes: tuple[ProcSample, ...]
