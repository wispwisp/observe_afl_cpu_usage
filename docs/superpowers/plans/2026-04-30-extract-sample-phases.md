# Extract `sample()` phases — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `PsutilTreeSampler.sample()` so each of its four
comment-labeled phases lives in a named module-level helper (Phase 4
splits into two), making `sample()` a short top-down orchestrator.

**Architecture:** Single-file refactor of
`src/cpu_process_tree_monitor/sampler.py`. Five new private
module-level functions (`_discover_tree`, `_read_proc_samples`,
`_aggregate_per_root`, `_aggregate_overall`,
`_select_top_processes`) replace the inline Phase 1–4 blocks. Existing
inline rationale comments move into per-helper docstrings. No public
API change, no behavior change.

**Tech Stack:** Python 3.9, `psutil`, Pydantic v2 (already used by
`samples.py`). Linux-only.

**Spec:** `docs/superpowers/specs/2026-04-30-extract-sample-phases-design.md`

---

## Project conventions you must follow

These come from `CLAUDE.md` and override the writing-plans skill's
defaults:

- **No test suite.** Don't add `pytest`, don't create `tests/`, don't
  add `[tool.pytest.ini_options]`. Verification = `python -m py_compile`
  per task + a final `docker build` / `docker run` performed by the
  user (not you).
- **Don't run long-running commands.** No `pip install`, `docker build`,
  `docker run`. `python -m py_compile` and `python -c "import ..."` are
  fine (sub-second).
- **Comments default to none.** Only keep a comment when it explains a
  non-obvious *why*. Move surviving rationale comments into helper
  docstrings, not inline comments.
- **Public surface = `__init__.py` re-exports** — these MUST NOT
  change. Don't add `_discover_tree` etc. to `__all__`.
- **Git identity for commits.** This repo has no `user.email` /
  `user.name` configured globally or locally; existing commits use
  `wisp <you@example.com>`. Use `GIT_AUTHOR_*` / `GIT_COMMITTER_*`
  env vars on each `git commit` rather than running
  `git config`. Commit command shown in each step.

---

## File map

Only one source file is modified across all five tasks:

- **Modify:** `src/cpu_process_tree_monitor/sampler.py`

Files explicitly NOT touched: `src/cpu_process_tree_monitor/__init__.py`,
`src/cpu_process_tree_monitor/monitor.py`,
`src/cpu_process_tree_monitor/samples.py`, `docker_demo/runner.py`,
`CLAUDE.md`, `README.md`, `pyproject.toml`.

---

## Reference: original `sample()`

For your reference while implementing, here is the current code in
`src/cpu_process_tree_monitor/sampler.py` lines 31–126 (the `sample`
method). Each task replaces one phase block from this code.

```python
def sample(self, *, interval_s: float, top_n: int) -> Sample:
    # Capture wall time once so every PID in this Sample shares one
    # timestamp regardless of how long the per-PID work below takes.
    wall_now = time.time()

    # Phase 1 - tree discovery. psutil performs the /proc walk.
    # A root that is dead/gone/inaccessible contributes an empty
    # descendant list; its RootSample later reports root_alive=False.
    per_root_descendants: dict[int, list[int]] = {}
    all_pids: set[int] = set()
    for root_pid in self._root_pids:
        desc: list[int] = []
        try:
            proc = psutil.Process(root_pid)
            # children(recursive=True) walks the tree at call time;
            # short-lived grandchildren that fork and exit between
            # samples will be missed here, but their CPU is still
            # captured via their (still-alive) parent's children_*.
            desc = [d.pid for d in proc.children(recursive=True)]
        except psutil.Error:
            pass
        per_root_descendants[root_pid] = desc
        all_pids.add(root_pid)
        all_pids.update(desc)

    # Phase 2 - per-PID cpu_times read. cpu_times() returns absolute
    # cumulative seconds since process start; first call on a fresh
    # psutil.Process is just as valid as later calls (unlike
    # cpu_percent, which needed priming). PIDs that vanish between
    # Phase 1 and this read (race with process exit) are silently
    # dropped. comm is truncated to 15 chars to match the
    # /proc/<pid>/comm kernel limit.
    proc_samples: dict[int, ProcSample] = {}
    for pid in all_pids:
        try:
            proc = psutil.Process(pid)
            t = proc.cpu_times()
            name = proc.name()
        except psutil.Error:
            continue
        proc_samples[pid] = ProcSample(
            pid=pid,
            comm=name[:15],
            cpu_times=CpuTimes(
                user_seconds=t.user,
                system_seconds=t.system,
                children_user_seconds=t.children_user,
                children_system_seconds=t.children_system,
            ),
        )

    # Phase 3 - per-root sub-aggregates. For each root, sum CpuTimes
    # over (root + descendants) PIDs that produced a sample. A
    # zombie/already-reaped root yields root_alive=False but its
    # surviving descendants still contribute, so the caller can see
    # leftover work in the tree even after the root itself exits.
    roots = tuple(
        RootSample(
            root_pid=root_pid,
            root_alive=root_pid in proc_samples,
            descendant_count=len(desc),
            aggregate=_sum_cpu_times(
                proc_samples[p].cpu_times
                for p in (root_pid, *desc)
                if p in proc_samples
            ),
        )
        for root_pid, desc in per_root_descendants.items()
    )

    # Phase 4 - cross-tree aggregate (deduped by pid via dict keys
    # so a descendant shared between two roots is counted once) and
    # top-N hottest by cumulative lifetime CPU. Note "top by total
    # cumulative" differs from the old "top by current rate"; the
    # AFL master will usually dominate the list because it's been
    # running longest.
    overall = _sum_cpu_times(p.cpu_times for p in proc_samples.values())
    top = tuple(sorted(
        proc_samples.values(),
        key=lambda p: (
            p.cpu_times.user_seconds
            + p.cpu_times.system_seconds
            + p.cpu_times.children_user_seconds
            + p.cpu_times.children_system_seconds
        ),
        reverse=True,
    )[:top_n])

    return Sample(
        timestamp_unix=wall_now,
        interval_s=interval_s,
        roots=roots,
        process_count=len(proc_samples),
        aggregate=overall,
        top_processes=top,
    )
```

The pre-existing module helper `_sum_cpu_times` (lines 129–141) stays
untouched and is reused by `_aggregate_per_root` and
`_aggregate_overall`.

---

## Final shape of `sample()` (after Task 5)

This is what `sample()` must look like after Task 5 commits. Each task
incrementally moves the body toward this state.

```python
def sample(self, *, interval_s: float, top_n: int) -> Sample:
    # Capture wall time once so every PID in this Sample shares one
    # timestamp regardless of how long the per-PID work below takes.
    wall_now = time.time()
    per_root_descendants, all_pids = _discover_tree(self._root_pids)
    proc_samples = _read_proc_samples(all_pids)
    roots = _aggregate_per_root(per_root_descendants, proc_samples)
    overall = _aggregate_overall(proc_samples)
    top = _select_top_processes(proc_samples, top_n)
    return Sample(
        timestamp_unix=wall_now,
        interval_s=interval_s,
        roots=roots,
        process_count=len(proc_samples),
        aggregate=overall,
        top_processes=top,
    )
```

---

## Per-task verification command

Every task ends with the same verification before committing. Run this
exact command from the repo root:

```bash
python -m py_compile src/cpu_process_tree_monitor/sampler.py && \
  python -c "from cpu_process_tree_monitor.sampler import PsutilTreeSampler, _sum_cpu_times; print('ok')"
```

Expected output: `ok` and exit status 0. Anything else → don't commit;
fix the file and re-run.

This requires `psutil` and `pydantic` installed. If the import fails
with `ModuleNotFoundError`, the executing agent should report the
missing dependency to the user — do NOT run `pip install` (project
convention).

To run from a path where the package isn't installed in dev mode:
```bash
PYTHONPATH=src python -m py_compile src/cpu_process_tree_monitor/sampler.py && \
  PYTHONPATH=src python -c "from cpu_process_tree_monitor.sampler import PsutilTreeSampler, _sum_cpu_times; print('ok')"
```

Use whichever form works in the current environment.

---

### Task 1: Extract `_discover_tree`

**Files:**
- Modify: `src/cpu_process_tree_monitor/sampler.py`

- [ ] **Step 1: Add the `_discover_tree` helper at module level**

Use Edit to insert this function in
`src/cpu_process_tree_monitor/sampler.py` immediately above the
existing `_sum_cpu_times` function (which currently starts at
line 129). Result: `_discover_tree` appears between the closing of
the `PsutilTreeSampler` class and `def _sum_cpu_times`.

```python
def _discover_tree(
    root_pids: list[int],
) -> tuple[dict[int, list[int]], set[int]]:
    """Walk each root's descendant tree via psutil.

    A dead/gone/inaccessible root contributes an empty descendant
    list; its root_pid is still added to all_pids so
    _read_proc_samples will attempt it and _aggregate_per_root can
    later report root_alive=False. children(recursive=True) walks
    the tree at call time, so short-lived grandchildren born and
    reaped between ticks are missed here, but their CPU is still
    captured via their (still-alive) parent's children_*.
    """
    per_root_descendants: dict[int, list[int]] = {}
    all_pids: set[int] = set()
    for root_pid in root_pids:
        desc: list[int] = []
        try:
            proc = psutil.Process(root_pid)
            desc = [d.pid for d in proc.children(recursive=True)]
        except psutil.Error:
            pass
        per_root_descendants[root_pid] = desc
        all_pids.add(root_pid)
        all_pids.update(desc)
    return per_root_descendants, all_pids
```

- [ ] **Step 2: Replace the Phase 1 block in `sample()`**

Use Edit to replace this exact block in `sample()`:

```python
    # Phase 1 - tree discovery. psutil performs the /proc walk.
    # A root that is dead/gone/inaccessible contributes an empty
    # descendant list; its RootSample later reports root_alive=False.
    per_root_descendants: dict[int, list[int]] = {}
    all_pids: set[int] = set()
    for root_pid in self._root_pids:
        desc: list[int] = []
        try:
            proc = psutil.Process(root_pid)
            # children(recursive=True) walks the tree at call time;
            # short-lived grandchildren that fork and exit between
            # samples will be missed here, but their CPU is still
            # captured via their (still-alive) parent's children_*.
            desc = [d.pid for d in proc.children(recursive=True)]
        except psutil.Error:
            pass
        per_root_descendants[root_pid] = desc
        all_pids.add(root_pid)
        all_pids.update(desc)
```

with this single line:

```python
    per_root_descendants, all_pids = _discover_tree(self._root_pids)
```

- [ ] **Step 3: Verify syntax + import**

Run the per-task verification command (see "Per-task verification
command" section above). Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
GIT_AUTHOR_NAME='wisp' GIT_AUTHOR_EMAIL='you@example.com' \
GIT_COMMITTER_NAME='wisp' GIT_COMMITTER_EMAIL='you@example.com' \
git -C /home/claudeuser/workspace commit -am "$(cat <<'EOF'
extract _discover_tree from sample()

Phase 1 (tree discovery) becomes a module-level helper. sample()
now calls it instead of inlining the psutil walk. Rationale comments
move from inline into the helper's docstring.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: `[use_cpu_times <hash>] extract _discover_tree from sample()`
with `1 file changed`.

---

### Task 2: Extract `_read_proc_samples`

**Files:**
- Modify: `src/cpu_process_tree_monitor/sampler.py`

- [ ] **Step 1: Add the `_read_proc_samples` helper at module level**

Insert this function immediately after `_discover_tree` (added in
Task 1) and before `_sum_cpu_times`.

```python
def _read_proc_samples(pids: Iterable[int]) -> dict[int, ProcSample]:
    """Read cumulative CPU times and comm for each PID.

    cpu_times() returns absolute cumulative seconds since process
    start; the first call on a fresh psutil.Process is correct
    without priming (unlike cpu_percent, which needed priming).
    PIDs that vanish between _discover_tree and this read (race with
    process exit) are silently dropped. comm is truncated to 15
    chars to match the /proc/<pid>/comm kernel limit.
    """
    proc_samples: dict[int, ProcSample] = {}
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            t = proc.cpu_times()
            name = proc.name()
        except psutil.Error:
            continue
        proc_samples[pid] = ProcSample(
            pid=pid,
            comm=name[:15],
            cpu_times=CpuTimes(
                user_seconds=t.user,
                system_seconds=t.system,
                children_user_seconds=t.children_user,
                children_system_seconds=t.children_system,
            ),
        )
    return proc_samples
```

Note: `Iterable` is already imported at the top of the file
(`from typing import Iterable`), so no new import is required.

- [ ] **Step 2: Replace the Phase 2 block in `sample()`**

Replace this exact block:

```python
    # Phase 2 - per-PID cpu_times read. cpu_times() returns absolute
    # cumulative seconds since process start; first call on a fresh
    # psutil.Process is just as valid as later calls (unlike
    # cpu_percent, which needed priming). PIDs that vanish between
    # Phase 1 and this read (race with process exit) are silently
    # dropped. comm is truncated to 15 chars to match the
    # /proc/<pid>/comm kernel limit.
    proc_samples: dict[int, ProcSample] = {}
    for pid in all_pids:
        try:
            proc = psutil.Process(pid)
            t = proc.cpu_times()
            name = proc.name()
        except psutil.Error:
            continue
        proc_samples[pid] = ProcSample(
            pid=pid,
            comm=name[:15],
            cpu_times=CpuTimes(
                user_seconds=t.user,
                system_seconds=t.system,
                children_user_seconds=t.children_user,
                children_system_seconds=t.children_system,
            ),
        )
```

with this single line:

```python
    proc_samples = _read_proc_samples(all_pids)
```

- [ ] **Step 3: Verify syntax + import**

Run the per-task verification command. Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
GIT_AUTHOR_NAME='wisp' GIT_AUTHOR_EMAIL='you@example.com' \
GIT_COMMITTER_NAME='wisp' GIT_COMMITTER_EMAIL='you@example.com' \
git -C /home/claudeuser/workspace commit -am "$(cat <<'EOF'
extract _read_proc_samples from sample()

Phase 2 (per-PID cpu_times read) becomes a module-level helper.
Rationale (cumulative-since-start, vanish-race drop, /proc/comm
15-char limit) moves to the helper's docstring.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: `1 file changed`.

---

### Task 3: Extract `_aggregate_per_root`

**Files:**
- Modify: `src/cpu_process_tree_monitor/sampler.py`

- [ ] **Step 1: Add the `_aggregate_per_root` helper at module level**

Insert this function immediately after `_read_proc_samples` and
before `_sum_cpu_times`.

```python
def _aggregate_per_root(
    per_root_descendants: dict[int, list[int]],
    proc_samples: dict[int, ProcSample],
) -> tuple[RootSample, ...]:
    """Build one RootSample per root.

    For each root, sum CpuTimes over (root + descendants) PIDs that
    produced a sample. A zombie/already-reaped root yields
    root_alive=False but its surviving descendants still contribute
    to its aggregate, so the caller can see leftover work in the
    tree even after the root itself exits.
    """
    return tuple(
        RootSample(
            root_pid=root_pid,
            root_alive=root_pid in proc_samples,
            descendant_count=len(desc),
            aggregate=_sum_cpu_times(
                proc_samples[p].cpu_times
                for p in (root_pid, *desc)
                if p in proc_samples
            ),
        )
        for root_pid, desc in per_root_descendants.items()
    )
```

- [ ] **Step 2: Replace the Phase 3 block in `sample()`**

Replace this exact block:

```python
    # Phase 3 - per-root sub-aggregates. For each root, sum CpuTimes
    # over (root + descendants) PIDs that produced a sample. A
    # zombie/already-reaped root yields root_alive=False but its
    # surviving descendants still contribute, so the caller can see
    # leftover work in the tree even after the root itself exits.
    roots = tuple(
        RootSample(
            root_pid=root_pid,
            root_alive=root_pid in proc_samples,
            descendant_count=len(desc),
            aggregate=_sum_cpu_times(
                proc_samples[p].cpu_times
                for p in (root_pid, *desc)
                if p in proc_samples
            ),
        )
        for root_pid, desc in per_root_descendants.items()
    )
```

with this single line:

```python
    roots = _aggregate_per_root(per_root_descendants, proc_samples)
```

- [ ] **Step 3: Verify syntax + import**

Run the per-task verification command. Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
GIT_AUTHOR_NAME='wisp' GIT_AUTHOR_EMAIL='you@example.com' \
GIT_COMMITTER_NAME='wisp' GIT_COMMITTER_EMAIL='you@example.com' \
git -C /home/claudeuser/workspace commit -am "$(cat <<'EOF'
extract _aggregate_per_root from sample()

Phase 3 (per-root sub-aggregates) becomes a module-level helper.
Zombie-root behavior moves into the helper's docstring.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: `1 file changed`.

---

### Task 4: Extract `_aggregate_overall` and `_select_top_processes`

This task splits the original Phase 4 into two helpers (cross-tree
aggregate + top-N selection) per the spec. Both extractions happen
in one task / one commit.

**Files:**
- Modify: `src/cpu_process_tree_monitor/sampler.py`

- [ ] **Step 1: Add the `_aggregate_overall` helper at module level**

Insert this function immediately after `_aggregate_per_root` and
before `_sum_cpu_times`.

```python
def _aggregate_overall(proc_samples: dict[int, ProcSample]) -> CpuTimes:
    """Sum CpuTimes across all sampled PIDs.

    Auto-deduped by pid via the dict keys: a descendant shared
    between two roots is counted once. This is the cross-tree
    aggregate, distinct from the per-root sub-aggregates produced
    by _aggregate_per_root.
    """
    return _sum_cpu_times(p.cpu_times for p in proc_samples.values())
```

- [ ] **Step 2: Add the `_select_top_processes` helper at module level**

Insert this function immediately after `_aggregate_overall` and
before `_sum_cpu_times`.

```python
def _select_top_processes(
    proc_samples: dict[int, ProcSample],
    top_n: int,
) -> tuple[ProcSample, ...]:
    """Return the top-N hottest ProcSamples by cumulative lifetime CPU.

    "Top by total cumulative" — the AFL master will usually
    dominate because it's been running longest. This is not "top
    by current rate".
    """
    return tuple(sorted(
        proc_samples.values(),
        key=lambda p: (
            p.cpu_times.user_seconds
            + p.cpu_times.system_seconds
            + p.cpu_times.children_user_seconds
            + p.cpu_times.children_system_seconds
        ),
        reverse=True,
    )[:top_n])
```

- [ ] **Step 3: Replace the Phase 4 block in `sample()`**

Replace this exact block:

```python
    # Phase 4 - cross-tree aggregate (deduped by pid via dict keys
    # so a descendant shared between two roots is counted once) and
    # top-N hottest by cumulative lifetime CPU. Note "top by total
    # cumulative" differs from the old "top by current rate"; the
    # AFL master will usually dominate the list because it's been
    # running longest.
    overall = _sum_cpu_times(p.cpu_times for p in proc_samples.values())
    top = tuple(sorted(
        proc_samples.values(),
        key=lambda p: (
            p.cpu_times.user_seconds
            + p.cpu_times.system_seconds
            + p.cpu_times.children_user_seconds
            + p.cpu_times.children_system_seconds
        ),
        reverse=True,
    )[:top_n])
```

with these two lines:

```python
    overall = _aggregate_overall(proc_samples)
    top = _select_top_processes(proc_samples, top_n)
```

- [ ] **Step 4: Verify syntax + import**

Run the per-task verification command. Expected: `ok`.

- [ ] **Step 5: Commit**

```bash
GIT_AUTHOR_NAME='wisp' GIT_AUTHOR_EMAIL='you@example.com' \
GIT_COMMITTER_NAME='wisp' GIT_COMMITTER_EMAIL='you@example.com' \
git -C /home/claudeuser/workspace commit -am "$(cat <<'EOF'
split phase 4 into _aggregate_overall and _select_top_processes

Phase 4 bundled cross-tree aggregation with top-N selection. They
are independent steps with different shapes, so they get separate
named helpers. Rationale (dedup-by-dict; "top by cumulative" not
"top by rate") moves into each helper's docstring.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: `1 file changed`.

---

### Task 5: Final cleanup of `sample()` and acceptance check

After Task 4, `sample()` should already be in its final shape — but
this task verifies that and tidies any residue (stray blank lines
between former phases, leftover `# Phase N` text the prior tasks
might have missed if the original file diverged from the reference
above).

**Files:**
- Modify: `src/cpu_process_tree_monitor/sampler.py` (verification
  only — usually no changes needed if Tasks 1–4 followed the spec)

- [ ] **Step 1: Read the current `sample()` body**

Read `src/cpu_process_tree_monitor/sampler.py`. Confirm the entire
`sample` method matches this exactly (modulo whitespace consistent
with the rest of the file):

```python
def sample(self, *, interval_s: float, top_n: int) -> Sample:
    # Capture wall time once so every PID in this Sample shares one
    # timestamp regardless of how long the per-PID work below takes.
    wall_now = time.time()
    per_root_descendants, all_pids = _discover_tree(self._root_pids)
    proc_samples = _read_proc_samples(all_pids)
    roots = _aggregate_per_root(per_root_descendants, proc_samples)
    overall = _aggregate_overall(proc_samples)
    top = _select_top_processes(proc_samples, top_n)
    return Sample(
        timestamp_unix=wall_now,
        interval_s=interval_s,
        roots=roots,
        process_count=len(proc_samples),
        aggregate=overall,
        top_processes=top,
    )
```

If there are extra blank lines between former phase boundaries
(e.g., a blank line between `wall_now = time.time()` and the
`_discover_tree` call, or between any of the helper calls), remove
them with Edit so the body is contiguous as shown.

- [ ] **Step 2: Grep for any leftover `# Phase` text**

```bash
grep -nE '# *Phase' src/cpu_process_tree_monitor/sampler.py || echo "clean"
```

Expected: `clean`. If grep finds matches, remove the offending
comments with Edit.

- [ ] **Step 3: Verify acceptance criteria**

Run these checks. Each must pass. Each `python -c` invocation may
need the `PYTHONPATH=src` prefix if the package is not installed in
dev mode — use the same form that worked for the per-task
verification command above.

a) `sample()` body is ≤ 20 lines:
```bash
python -c "
import ast, pathlib
src = pathlib.Path('src/cpu_process_tree_monitor/sampler.py').read_text()
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == 'sample':
        lines = node.end_lineno - node.lineno + 1
        print(f'sample() spans {lines} lines (target: ≤ 20)')
        assert lines <= 20, lines
print('ok')
"
```
Expected: `sample() spans <N> lines (target: ≤ 20)` and `ok`.

b) Five new helpers exist:
```bash
python -c "
from cpu_process_tree_monitor.sampler import (
    _discover_tree,
    _read_proc_samples,
    _aggregate_per_root,
    _aggregate_overall,
    _select_top_processes,
    _sum_cpu_times,
    PsutilTreeSampler,
)
print('ok')
"
```
Expected: `ok`. (Use `PYTHONPATH=src` prefix if needed.)

c) Public surface unchanged:
```bash
python -c "
import cpu_process_tree_monitor as p
assert set(p.__all__) == {
    '__version__','CpuTreeMonitor','PsutilTreeSampler',
    'CpuTimes','ProcSample','RootSample','Sample','SampleCallback',
}, p.__all__
print('ok')
"
```
Expected: `ok`.

d) Each new helper has a docstring:
```bash
python -c "
from cpu_process_tree_monitor import sampler
for name in ['_discover_tree','_read_proc_samples','_aggregate_per_root','_aggregate_overall','_select_top_processes']:
    fn = getattr(sampler, name)
    assert fn.__doc__ and fn.__doc__.strip(), name
print('ok')
"
```
Expected: `ok`.

e) Only `sampler.py` modified across the four refactor commits:
```bash
git -C /home/claudeuser/workspace diff --stat HEAD~4..HEAD
```
Expected: a single file `src/cpu_process_tree_monitor/sampler.py`
in the stat. (Run this before any Step 4 cleanup commit, otherwise
adjust to `HEAD~5..HEAD`.)

- [ ] **Step 4: Commit if there were edits in Steps 1–2**

If Edits were made in Steps 1 or 2 (residue cleanup), commit:

```bash
GIT_AUTHOR_NAME='wisp' GIT_AUTHOR_EMAIL='you@example.com' \
GIT_COMMITTER_NAME='wisp' GIT_COMMITTER_EMAIL='you@example.com' \
git -C /home/claudeuser/workspace commit -am "$(cat <<'EOF'
clean up leftover phase-comment residue in sample()

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

If no edits were needed, skip this step (the previous four commits
already produced the target state).

- [ ] **Step 5: Hand off to the user for docker verification**

Per `CLAUDE.md`, the implementing agent does not run `docker build`
or `docker run`. Report to the user:

> Refactor complete on branch `use_cpu_times`. `sample()` body is
> N lines (target ≤ 20). Five helpers added with docstrings.
> Acceptance checks (a)–(e) above all passed. Please run:
> ```
> docker build -t cpu-process-tree-monitor-demo -f docker_demo/Dockerfile .
> docker run --rm -it cpu-process-tree-monitor-demo
> ```
> and confirm `emit_to_otel` log lines still show non-zero
> aggregate CPU and `afl-fuzz` / `target` in the top processes
> list.

---

## Rollback

If a task's verification fails and you can't quickly identify the
cause:

```bash
git -C /home/claudeuser/workspace reset --hard HEAD~1
```

This undoes the most recent commit on `use_cpu_times`. Confirm with
the user before running this — it discards work, and `git reset
--hard` is destructive (per CLAUDE.md / project conventions, ask
first).
