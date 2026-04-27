# syntax=docker/dockerfile:1.6
#
# cpu-process-tree-monitor smoke-test image. Runs AFL++ against a deliberately
# crashing C++ target while the Python CPU tree monitor watches AFL's
# pid tree. Outputs one JSONL sample per second to /out/cpu.jsonl.
#
# Build (from repo root, NOT from docker_demo/):
#   docker build -t cpu-process-tree-monitor-demo -f docker_demo/Dockerfile .
#
# Run:
#   docker run --rm -it -v "$PWD/out:/out" cpu-process-tree-monitor-demo
#

FROM aflplusplus/aflplusplus:v4.21c AS afl-build
WORKDIR /work
COPY docker_demo/target/ ./target/
RUN cd target && afl-clang-fast++ -O2 -o target target.cc

FROM aflplusplus/aflplusplus:v4.21c

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app

# Use a venv so we don't fight PEP 668 / system-managed Python and so the
# install works against pip versions both older and newer than 23.0.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

COPY --from=afl-build /work/target/target /opt/app/target/target
COPY docker_demo/seeds/        /opt/app/seeds/
COPY docker_demo/runner.py     /opt/app/runner.py
COPY docker_demo/entrypoint.sh /opt/app/entrypoint.sh
RUN chmod +x /opt/app/entrypoint.sh

ENV AFL_SKIP_CPUFREQ=1 \
    AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
    AFL_BENCH_UNTIL_CRASH=1

VOLUME ["/out"]
ENTRYPOINT ["/opt/app/entrypoint.sh"]
