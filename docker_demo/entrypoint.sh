#!/usr/bin/env bash
set -eu

# AFL pre-flight: relax core_pattern if the FS lets us. Hardened hosts make
# this read-only; AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES is set in the
# Dockerfile to keep AFL from refusing to start.
if [ -w /proc/sys/kernel/core_pattern ]; then
    echo core > /proc/sys/kernel/core_pattern || true
fi

exec python3 /opt/app/runner.py "$@"
