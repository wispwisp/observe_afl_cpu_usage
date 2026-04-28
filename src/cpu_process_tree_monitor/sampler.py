"""psutil-based CPU sampler for one or more process trees."""
from __future__ import annotations

import time
from typing import Iterable

import psutil

from .samples import ProcSample, RootSample, Sample


class PsutilTreeSampler:
    """Walks the descendant tree(s) of one or more root PIDs and produces a
    Sample each tick using psutil.Process.cpu_percent (delta-based,
    non-blocking). A per-PID psutil.Process cache is kept across ticks
    because cpu_percent(interval=None) is computed against the previous
    call on the same instance.
    """

    def __init__(self, root_pids: Iterable[int]) -> None:
        # Normalize the caller's input: coerce to int, dedupe while
        # preserving insertion order (so per-root output keeps the
        # order the caller passed, which matters for AFL -M/-S where
        # the main fuzzer is conventionally first), and reject the
        # empty case here rather than letting sample() silently
        # produce a useless aggregate later.
        deduped = list(dict.fromkeys(int(p) for p in root_pids))
        if not deduped:
            raise ValueError("root_pids must be non-empty")
        self._root_pids: list[int] = deduped
        # Why _cache: psutil.Process.cpu_percent(interval=None) is delta-based.
        # It returns (cpu_time_now − cpu_time_at_previous_call) / wall_elapsed computed
        # against the previous call on the same psutil.Process instance. The first call
        # on any fresh instance has no "previous" — it always returns 0.0.
        self._cache: dict[int, psutil.Process] = {}
        self._ncpu = psutil.cpu_count() or 1
        # Prime each root at construction time. cpu_percent(interval=None)
        # always returns 0.0 on its first call against a Process instance
        # (no prior reference point exists yet), so we spend that first
        # call here. Without priming, the very first sample() tick
        # would report 0% CPU for every root.
        for pid in self._root_pids:
            self._prime(pid)

    def _prime(self, pid: int) -> psutil.Process | None:
        # Get-or-create-and-seed for the per-PID Process cache. On a
        # cache miss we construct a fresh psutil.Process and burn its
        # mandatory 0.0 first cpu_percent reading so subsequent calls
        # against the cached instance yield real CPU deltas. A PID
        # that has died or is inaccessible (permissions, race with
        # exit) returns None so callers can treat the PID as "no
        # sample this tick" instead of raising.
        proc = self._cache.get(pid)
        if proc is not None:
            return proc
        try:
            proc = psutil.Process(pid)
            proc.cpu_percent(interval=None)
        except psutil.Error:
            return None
        self._cache[pid] = proc
        return proc

    def sample(self, *, interval_s: float, top_n: int) -> Sample:
        # Capture wall time once up front so every PID in this Sample
        # shares a single timestamp regardless of how long the per-PID
        # work below takes.
        wall_now = time.time()

        # Phase 1 - tree discovery. For each root, ask psutil for its
        # full recursive descendant list; psutil performs the /proc
        # walk internally so we don't hand-roll one. A root that is
        # dead, gone, or inaccessible contributes an empty descendant
        # list - its RootSample later in this tick will report
        # root_alive=False with a zero aggregate (Phase 2 fails to
        # read its cpu_percent and silently drops it). The
        # per_root_descendants mapping is kept so we can later compute
        # sub-aggregates without re-walking the tree.
        per_root_descendants: dict[int, list[int]] = {}
        all_pids: set[int] = set()
        for root_pid in self._root_pids:
            proc = self._prime(root_pid)
            desc: list[int] = []
            if proc is not None:
                try:
                    # children(recursive=True) walks the tree at the moment you call it.
                    # Short-lived grandchildren that fork and exit between samples will be missed.
                    desc = [d.pid for d in proc.children(recursive=True)]
                except psutil.Error:
                    pass
            per_root_descendants[root_pid] = desc
            all_pids.add(root_pid)
            all_pids.update(desc)

        # Phase 2 - per-PID CPU and name read for everything in any
        # tree. cpu_percent(interval=None) is non-blocking and returns
        # the CPU delta since the previous call on the same Process
        # instance, which is why self._cache is load-bearing rather
        # than a mere optimization. The comm field is truncated to 15
        # chars to match the /proc/<pid>/comm kernel limit so
        # downstream consumers see the familiar short name. PIDs that
        # vanish between Phase 1 and this read (race with process
        # exit) are silently dropped.
        proc_samples: dict[int, ProcSample] = {}
        for pid in all_pids:
            proc = self._prime(pid)
            if proc is None:
                continue
            try:
                cpu = float(proc.cpu_percent(interval=None))
                name = proc.name()
            except psutil.Error:
                continue
            proc_samples[pid] = ProcSample(pid=pid, comm=name[:15], cpu_percent=cpu)

        # Phase 3 - cache pruning. Without this the cache would grow
        # unbounded for workloads like AFL, which spawn and reap target
        # binaries continuously. We retain entries that are still part
        # of any tree this tick and evict everything else; an evicted
        # PID that reappears later will be re-primed on the next tick.
        self._cache = {pid: proc for pid, proc in self._cache.items() if pid in all_pids}

        # Phase 4 - per-root sub-aggregates. For each root, sum the
        # CPU of every (root + descendant) PID that produced a sample
        # in Phase 2. The root_alive flag reflects only whether the
        # root itself reported this tick: a zombie or already-reaped
        # root yields root_alive=False, but its surviving descendants
        # still contribute to aggregate_cpu_percent so the caller can
        # see leftover work in the tree even after the root exits.
        roots = tuple(
            RootSample(
                root_pid=root_pid,
                root_alive=root_pid in proc_samples,
                descendant_count=len(desc),
                aggregate_cpu_percent=sum(
                    proc_samples[p].cpu_percent
                    for p in (root_pid, *desc)
                    if p in proc_samples
                ),
            )
            for root_pid, desc in per_root_descendants.items()
        )

        # Phase 5 - cross-tree aggregates and the top-N hottest
        # processes. normalized_cpu_percent divides the raw aggregate
        # by ncpu so 100% always means "one core's worth saturated"
        # regardless of host width, which is the scale the
        # OpenTelemetry consumer on the receiving side expects.
        # top_processes is computed across every tree so the hottest
        # offender surfaces no matter which fuzzer parent spawned it.
        aggregate = sum(p.cpu_percent for p in proc_samples.values())
        top = tuple(
            sorted(proc_samples.values(), key=lambda p: p.cpu_percent, reverse=True)[:top_n]
        )
        return Sample(
            timestamp_unix=wall_now,
            interval_s=interval_s,
            ncpu=self._ncpu,
            roots=roots,
            process_count=len(proc_samples),
            aggregate_cpu_percent=aggregate,
            normalized_cpu_percent=aggregate / self._ncpu,
            top_processes=top,
        )
