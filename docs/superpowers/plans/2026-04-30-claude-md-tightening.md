# CLAUDE.md tightening — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current 122-line `CLAUDE.md` with a ~50-line version that preserves every load-bearing guardrail. The exact replacement text is fixed by the spec.

**Architecture:** Single-file rewrite via the Write tool. The previously-staged tweaks (`Python 3.10+` → `Python 3.9 only`; removal of the `-v "$PWD/out:/out"` volume mount from `docker run`) are already represented in the new content and fold into the same commit. No source code is touched.

**Tech Stack:** Markdown only. No build, no test suite (this project has none — see `CLAUDE.md` "Conventions"). Verification = `wc -l`, a few targeted greps, and reading the diff.

**Reference:** `docs/superpowers/specs/2026-04-30-claude-md-tightening-design.md`.

---

### Task 1: Rewrite CLAUDE.md

**Files:**
- Modify: `/home/claudeuser/workspace/CLAUDE.md` (full rewrite, replace whole file)

- [ ] **Step 1: Confirm starting state**

Run:

```bash
git -C /home/claudeuser/workspace status --short
git -C /home/claudeuser/workspace log --oneline -3
```

Expected `status --short`:
```
M  CLAUDE.md
?? .claude/
```
(`M ` with two spaces — staged but no further unstaged change. `.claude/` is the user's local untracked tooling and is irrelevant to this task.)

Expected `log --oneline -3` (top entry is the spec commit from the prior brainstorming step):
```
4392463 add CLAUDE.md tightening spec
bcfe1b4 use cpu_times()
c0cb3fc simplification
```

If `CLAUDE.md` is not staged with the prior tweaks, or HEAD is not `4392463`, stop and ask.

- [ ] **Step 2: Read the current CLAUDE.md to ground yourself**

Use the Read tool on `/home/claudeuser/workspace/CLAUDE.md`. You will see the full current file. Note that it is the staged version (with `Python 3.9 only` and the `docker run` line without the `-v` flag), not the HEAD version. This is expected.

- [ ] **Step 3: Overwrite CLAUDE.md with the new content**

Use the Write tool on `/home/claudeuser/workspace/CLAUDE.md` with the following exact content (everything inside the outer 6-tick fence, not including the fence itself):

``````markdown
# cpu-process-tree-monitor — project memory for future Claude sessions

## What this project is

A Python subsystem that monitors CPU usage of an AFL++ fuzzing process
and its descendant tree. Drops into existing infrastructure that already
launches AFL++ via `subprocess`. Each tick produces one `Sample` and
delivers it to a single `on_sample` callback supplied by the caller;
the production target is OpenTelemetry.

## Frozen design choices

These were chosen deliberately and survived two simplification passes.
Do not reintroduce removed pieces without asking the user.

- **`psutil` only; no per-PID cache.** Each tick walks
  `psutil.Process(root).children(recursive=True)` and reads `cpu_times()`
  per PID. `cpu_times()` is cumulative (monotonic counters, OTel-shaped),
  so a fresh `psutil.Process` per tick is correct and simpler than caching.
- **Single `on_sample` callback.** No `Sink` protocol, no fanout, no JSONL
  writer, no watchdog tailing. Failures inside the callback are caught
  and logged. The OTel adapter lives in the caller — see
  `docker_demo/runner.py:emit_to_otel` for the stand-in.
- **Multi-root is first-class.** `root_pids: list[int]`; `Sample` carries
  per-root sub-aggregates plus a global aggregate (parallel `-M`/`-S`).
- **No cgroup backend.** Sampler interface is kept clean so cgroup v2
  can be added later without API churn.
- **Lifecycle: one daemon thread driving a private `schedule.Scheduler()`.**
  `stop()` sets a `threading.Event` and joins. Restart after `stop()` is
  supported. No auto-stop on root exit — the runner owns that.
- **Library installs no signal handlers.** The runner owns SIGINT/SIGTERM
  and calls `monitor.stop()`.
- **Removed and not coming back:** PID-reuse defense, explicit zombie
  check, drift-corrected scheduling, `schema_version`, `Sink` protocol,
  JSONL writer, watchdog handler.
- **Scope is strictly CPU.** Memory, per-core breakdown, and threshold
  alerts are out of scope.

## Known limitation

The born-and-die-between-ticks gap is largely closed by `cpu_times()`'s
`children_user` / `children_system`: when the kernel reaps a child, its
CPU is added to the still-alive parent's `children_*`. AFL's master and
fork-server stay alive throughout, so target-binary CPU is captured via
the fork-server. Remaining narrow gap: an intermediate parent that exits
while a child is still alive reparents that child to init, which we
don't sample. Rare in AFL. A future cgroup v2 backend would close this.

## How to run the demo

```
docker build -t cpu-process-tree-monitor-demo -f docker_demo/Dockerfile .
docker run --rm -it cpu-process-tree-monitor-demo
```

Expect log lines from `emit_to_otel` with non-zero aggregate CPU and top
processes including `afl-fuzz` and `target`; AFL crashes within seconds.

## Conventions

- Python 3.9, type hints throughout. Linux-only on purpose (`psutil`
  reads `/proc`).
- **No automated test suite.** No `tests/` directory, no `pytest` in
  dependencies, no `[tool.pytest.ini_options]`. Verification = `docker
  build` / `docker run` + reading runner logs.
- The user runs builds/tests/dev-servers and pastes errors back. Do not
  run `pip install`, `docker build`, `docker run`, or other long-running
  commands unless asked.
- Public surface = the re-exports in
  `src/cpu_process_tree_monitor/__init__.py`. Everything else is internal.
``````

The file ends with the line beginning `src/cpu_process_tree_monitor/__init__.py`. There is no trailing line beyond that.

- [ ] **Step 4: Verify line count**

Run:

```bash
wc -l /home/claudeuser/workspace/CLAUDE.md
```

Expected: a number ≤ 60. The spec target is ~50; the acceptance ceiling is 60. If `wc -l` reports > 60, stop and reconcile against the spec — do not silently shorten or pad.

- [ ] **Step 5: Verify load-bearing content is preserved**

Run each of these greps. Each must return exactly one line:

```bash
cd /home/claudeuser/workspace
grep -nF '`psutil` only; no per-PID cache' CLAUDE.md
grep -nF 'Single `on_sample` callback' CLAUDE.md
grep -nF 'Multi-root is first-class' CLAUDE.md
grep -nF 'No cgroup backend' CLAUDE.md
grep -nF 'Lifecycle: one daemon thread' CLAUDE.md
grep -nF 'Library installs no signal handlers' CLAUDE.md
grep -nF 'Removed and not coming back' CLAUDE.md
grep -nF 'Scope is strictly CPU' CLAUDE.md
grep -nF 'Known limitation' CLAUDE.md
grep -nF 'No automated test suite' CLAUDE.md
grep -nF 'Linux-only on purpose' CLAUDE.md
grep -nF 'Python 3.9' CLAUDE.md
grep -nF 'Public surface' CLAUDE.md
```

Expected: each command prints one line of the form `N:<matched line>`. If any returns 0 or >1 matches, the file content drifted from the spec — re-run Step 3.

- [ ] **Step 6: Verify the previously-staged tweaks survived**

Run:

```bash
cd /home/claudeuser/workspace
grep -nF 'docker run --rm -it cpu-process-tree-monitor-demo' CLAUDE.md
grep -nF 'Python 3.9' CLAUDE.md
grep -nF 'docker run --rm -it -v' CLAUDE.md && echo 'FAIL: -v flag still present' || echo 'OK: -v flag removed'
grep -nF 'Python 3.10' CLAUDE.md && echo 'FAIL: 3.10 still present' || echo 'OK: 3.10 removed'
```

Expected: first two grep lines match exactly once; last two print the `OK: …` line.

- [ ] **Step 7: Confirm diff scope is just CLAUDE.md**

Run:

```bash
git -C /home/claudeuser/workspace status --short
```

Expected:
```
MM CLAUDE.md
?? .claude/
```
(`MM` means the file is modified in both index and worktree relative to HEAD. `.claude/` is untracked and untouched.)

If any other tracked file appears (`src/…`, `docker_demo/…`, `pyproject.toml`, `README.md`), stop — something went wrong; do not commit.

- [ ] **Step 8: Stage and commit**

```bash
cd /home/claudeuser/workspace
git add CLAUDE.md
git -c user.name='wisp' -c user.email='you@example.com' commit -m "$(cat <<'EOF'
tighten CLAUDE.md

See docs/superpowers/specs/2026-04-30-claude-md-tightening-design.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

The `-c user.name`/`-c user.email` flags match the existing repo author identity (`wisp <you@example.com>`) without touching `git config`. Do not run `git config` and do not change the global identity.

Expected output: `[use_cpu_times <hash>] tighten CLAUDE.md` with `1 file changed, …`.

- [ ] **Step 9: Final sanity check**

Run:

```bash
git -C /home/claudeuser/workspace log --oneline -4
git -C /home/claudeuser/workspace status --short
git -C /home/claudeuser/workspace diff HEAD~1 --stat
```

Expected:
- `log --oneline -4` shows the new commit at the top, then `add CLAUDE.md tightening spec`, `use cpu_times()`, `simplification`.
- `status --short` shows only `?? .claude/`.
- `diff HEAD~1 --stat` shows only `CLAUDE.md` changed.

If any of these is unexpected (e.g. extra files in the diff), report it; do not amend or force-push.

---

## Notes for the executor

- The spec doc is committed at HEAD (commit `4392463`). Do not modify or revert it.
- Do not invoke any other skill. Do not edit source code in `src/` or `docker_demo/`. Do not run `docker build`, `docker run`, or `pip install`.
- The CLAUDE.md instruction "Do not run … long-running commands unless asked" still applies. Builds and Docker invocations are not part of this plan.
- If you discover the spec's "Proposed new content" disagrees with the content embedded in Step 3 above, treat the content in Step 3 as authoritative for this implementation; flag the divergence to the user separately.
