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

from cpu_process_tree_monitor import CpuTreeMonitor, Sample


def emit_to_otel(sample: Sample) -> None:
    """Stand-in for an OpenTelemetry exporter call.

    In production this is where you'd record gauges / observable counters
    on a `metrics.Meter` and let the configured OTLP exporter push them.
    For the smoke test we just log a compact summary so `docker run`
    output shows samples flowing.
    """
    log = logging.getLogger("otel")
    top = ", ".join(
        f"{p.comm}({p.pid})={p.cpu_percent:.0f}%"
        for p in sample.top_processes[:3]
    )
    log.info(
        "[otel] agg=%.1f%% norm=%.1f%% procs=%d roots=%d top=[%s]",
        sample.aggregate_cpu_percent,
        sample.normalized_cpu_percent,
        sample.process_count,
        len(sample.roots),
        top,
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

    monitor = CpuTreeMonitor(
        root_pids=[afl.pid],
        on_sample=emit_to_otel,
        interval_s=args.interval,
        top_n=args.top_n,
        stop_when_all_roots_exit=True,
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
