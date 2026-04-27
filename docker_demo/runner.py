"""End-to-end smoke test: launch AFL++ against /opt/app/target/target and
attach the CPU tree monitor to AFL's PID, exercising all three integration
paths simultaneously:

  1. JsonlFileSink writes one sample per second to /out/cpu.jsonl
  2. LoggingSink prints a one-line summary per sample
  3. A watchdog Observer with a CpuSampleEventHandler subclass tails the
     JSONL file and dispatches parsed Sample objects to on_cpu_sample()

This proves both in-process integration (sink callbacks) and the headline
file-based integration (watchdog Observer + FileSystemEventHandler).
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

from watchdog.observers import Observer

from afl_cpu_monitor import (
    CpuSampleEventHandler,
    CpuTreeMonitor,
    JsonlFileSink,
    LoggingSink,
    Sample,
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
        shutil.rmtree(findings)
    jsonl = out / "cpu.jsonl"
    if jsonl.exists():
        jsonl.unlink()

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

    class DemoHandler(CpuSampleEventHandler):
        def on_cpu_sample(self, sample: Sample) -> None:
            top3 = [
                (p.pid, p.comm, round(p.cpu_percent, 1))
                for p in sample.top_processes[:3]
            ]
            log.info(
                "watchdog<-cpu agg=%.1f%% norm=%.1f%% procs=%d top=%s",
                sample.aggregate_cpu_percent,
                sample.normalized_cpu_percent,
                sample.process_count,
                top3,
            )

    handler = DemoHandler(jsonl)
    observer = Observer()
    observer.schedule(handler, str(jsonl.parent), recursive=False)
    observer.start()

    monitor = CpuTreeMonitor(
        root_pids=[afl.pid],
        sinks=[
            JsonlFileSink(jsonl),
            LoggingSink(log),
        ],
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
        observer.stop()
        observer.join(timeout=2.0)
        log.info("done; afl rc=%s, samples in %s", rc, jsonl)
    return rc if rc is not None else 0


if __name__ == "__main__":
    sys.exit(main())
