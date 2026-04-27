"""Sample dataclasses and JSON serialization."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ProcSample:
    pid: int
    ppid: int
    comm: str
    cpu_percent: float
    cpu_time_user_s: float
    cpu_time_system_s: float


@dataclass(frozen=True, slots=True)
class RootSample:
    root_pid: int
    root_alive: bool
    descendant_count: int
    aggregate_cpu_percent: float


@dataclass(frozen=True, slots=True)
class Sample:
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
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "timestamp_unix": self.timestamp_unix,
            "monotonic_s": self.monotonic_s,
            "interval_s": self.interval_s,
            "elapsed_s": self.elapsed_s,
            "ncpu": self.ncpu,
            "roots": [asdict(r) for r in self.roots],
            "process_count": self.process_count,
            "aggregate_cpu_percent": self.aggregate_cpu_percent,
            "normalized_cpu_percent": self.normalized_cpu_percent,
            "top_processes": [asdict(p) for p in self.top_processes],
            "sampling_overhead_ms": self.sampling_overhead_ms,
            "dropped_pids": self.dropped_pids,
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Sample":
        return cls(
            schema_version=d["schema_version"],
            timestamp_unix=d["timestamp_unix"],
            monotonic_s=d["monotonic_s"],
            interval_s=d["interval_s"],
            elapsed_s=d["elapsed_s"],
            ncpu=d["ncpu"],
            roots=tuple(RootSample(**r) for r in d["roots"]),
            process_count=d["process_count"],
            aggregate_cpu_percent=d["aggregate_cpu_percent"],
            normalized_cpu_percent=d["normalized_cpu_percent"],
            top_processes=tuple(ProcSample(**p) for p in d["top_processes"]),
            sampling_overhead_ms=d["sampling_overhead_ms"],
            dropped_pids=d["dropped_pids"],
            notes=tuple(d.get("notes", [])),
        )

    @classmethod
    def from_json(cls, line: str) -> "Sample":
        return cls.from_dict(json.loads(line))
