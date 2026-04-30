"""End-to-end smoke test: launch AFL++ against /opt/app/target/target,
attach the CPU tree monitor to AFL's PID, and stream each Sample to a
stand-in OpenTelemetry exporter (here: a logger).
"""
from __future__ import annotations

import argparse
import logging
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from process_tree_monitor import ProcessTreeMonitor, Sample


def emit_to_otel(sample: Sample) -> None:
    """Stand-in for an OpenTelemetry exporter call.

    In production this is where you'd record observable counters/gauges on
    a `metrics.Meter` (counters for cpu_times fields, gauges for memory
    fields) and let the configured OTLP exporter push them. For the smoke
    test we just log a compact summary so `docker run` output shows
    samples flowing.
    """
    log = logging.getLogger("otel")
    cpu = sample.aggregate
    mem = sample.memory_aggregate
    top_cpu = ", ".join(
        f"{p.comm}({p.pid})="
        f"u={p.cpu_times.user_seconds:.1f}s,"
        f"s={p.cpu_times.system_seconds:.1f}s"
        for p in sample.top_processes_by_cpu[:3]
    )
    top_mem = ", ".join(
        f"{p.comm}({p.pid})="
        f"rss={p.memory.rss_bytes // 1024}K"
        + (f",pss={p.memory.pss_bytes // 1024}K" if p.memory.pss_bytes is not None else "")
        for p in sample.top_processes_by_memory[:3]
    )
    pss_str = (
        f"{mem.pss_bytes // 1024}K"
        if mem.pss_bytes is not None else "n/a"
    )
    log.info(
        "[otel] cpu u=%.1fs s=%.1fs cu=%.1fs cs=%.1fs | "
        "mem rss=%dK vms=%dK pss=%s pid_count=%s | procs=%d roots=%d "
        "top_cpu=[%s] top_mem=[%s]",
        cpu.user_seconds,
        cpu.system_seconds,
        cpu.children_user_seconds,
        cpu.children_system_seconds,
        mem.rss_bytes // 1024,
        mem.vms_bytes // 1024,
        pss_str,
        sample.memory_full_info_pid_count,
        sample.process_count,
        len(sample.roots),
        top_cpu,
        top_mem,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="/out", type=Path)
    p.add_argument("--target", default="/opt/app/target/target")
    p.add_argument("--seeds", default="/opt/app/seeds")
    p.add_argument("--interval", default=1.0, type=float)
    p.add_argument("--top-n", default=10, type=int)
    p.add_argument(
        "--max-runtime",
        default=120,
        type=int,
        help="seconds before the runner terminates AFL itself",
    )
    p.add_argument(
        "--full-memory-info",
        action="store_true",
        help="enable USS/PSS/swap reads via memory_full_info() (slower)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("runner")

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    findings = out / "findings"
    if findings.exists():
        log.info("removing existing findings dir: %s", findings)
        shutil.rmtree(findings)

    afl_cmd = [
        "afl-fuzz",
        "-i", str(args.seeds),
        "-o", str(findings),
        "--", str(args.target),
    ]
    log.info("launching: %s", " ".join(afl_cmd))
    afl = subprocess.Popen(
        afl_cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    log.info("afl pid=%d", afl.pid)

    monitor = ProcessTreeMonitor(
        root_pids=[afl.pid],
        on_sample=emit_to_otel,
        interval_s=args.interval,
        top_n=args.top_n,
        full_memory_info=args.full_memory_info,
    )

    stop_event = threading.Event()

    def shutdown(signum, _frame):
        log.info("signal %d received; terminating afl", signum)
        stop_event.set()
        try:
            afl.terminate()
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    rc = 0
    monitor.start()
    deadline = time.monotonic() + args.max_runtime
    try:
        while not stop_event.is_set():
            if afl.poll() is not None:
                break
            if time.monotonic() > deadline:
                log.info("max-runtime reached; terminating afl")
                afl.terminate()
                break
            time.sleep(0.5)
        try:
            rc = afl.wait(timeout=10)
        except subprocess.TimeoutExpired:
            afl.kill()
            rc = afl.wait(timeout=5)
    finally:
        monitor.stop(timeout=5.0)
        log.info("done; afl rc=%s", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
