"""/proc helpers: CPU count, descendant-tree walking via pid->ppid map."""
from __future__ import annotations

import os
from collections import deque
from pathlib import Path
from typing import Iterable

PROC = Path("/proc")


def num_cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def read_pid_ppid_map() -> dict[int, int]:
    """One pass over /proc to build {pid: ppid}.

    The comm field in /proc/[pid]/stat may contain spaces and parentheses,
    so we split on the LAST ')' to find the rest of the fields.
    Entries that vanish or fail to parse mid-read are silently skipped.
    """
    result: dict[int, int] = {}
    try:
        entries = os.listdir(PROC)
    except OSError:
        return result
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(PROC / entry / "stat", "rb") as f:
                data = f.read()
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        try:
            rparen = data.rindex(b")")
        except ValueError:
            continue
        rest = data[rparen + 1 :].split()
        if len(rest) < 2:
            continue
        try:
            pid = int(entry)
            ppid = int(rest[1])
        except ValueError:
            continue
        result[pid] = ppid
    return result


def build_children_map(pid_to_ppid: dict[int, int]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for pid, ppid in pid_to_ppid.items():
        children.setdefault(ppid, []).append(pid)
    return children


def descendants(root: int, children_map: dict[int, list[int]]) -> set[int]:
    """All descendants of root (excluding root itself)."""
    out: set[int] = set()
    queue: deque[int] = deque([root])
    while queue:
        node = queue.popleft()
        for child in children_map.get(node, ()):
            if child not in out and child != root:
                out.add(child)
                queue.append(child)
    return out


def walk_tree(roots: Iterable[int]) -> set[int]:
    """Union of {roots} and all their descendants. Roots not in /proc are
    treated as dead (their descendants are still discovered if they exist
    elsewhere in the children map, but only via that root's surviving
    children — once orphaned to PID 1 they are no longer descendants)."""
    pid_to_ppid = read_pid_ppid_map()
    children = build_children_map(pid_to_ppid)
    out: set[int] = set()
    for r in roots:
        if r in pid_to_ppid:
            out.add(r)
        out.update(descendants(r, children))
    return out
