"""Process introspection helpers used by strict-mode cgroup supervision."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List, Set

PROC = Path("/proc")
MAX_TREE_DEPTH = 12


def comm_for(pid: int) -> str:
    try:
        return (PROC / str(pid) / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ppid_for(pid: int) -> int:
    try:
        stat = (PROC / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return 0
    # comm may contain spaces/parens: parse after the final ')'
    rparen = stat.rfind(")")
    if rparen == -1:
        return 0
    fields = stat[rparen + 2:].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return 0


def all_pids() -> List[int]:
    pids: List[int] = []
    try:
        for entry in os.listdir(PROC):
            if entry.isdigit():
                pids.append(int(entry))
    except OSError:
        pass
    return pids


def descendant_pids(root_pid: int, max_depth: int = MAX_TREE_DEPTH) -> Set[int]:
    """Return ``root_pid`` plus all of its descendants (bounded depth)."""
    pids = all_pids()
    children: Dict[int, List[int]] = {}
    for pid in pids:
        parent = ppid_for(pid)
        children.setdefault(parent, []).append(pid)

    result: Set[int] = {root_pid}
    frontier = [root_pid]
    depth = 0
    while frontier and depth < max_depth:
        next_frontier: List[int] = []
        for pid in frontier:
            for child in children.get(pid, []):
                if child not in result:
                    result.add(child)
                    next_frontier.append(child)
        frontier = next_frontier
        depth += 1
    return result


def find_pids_by_names(names: Iterable[str]) -> Dict[str, Set[int]]:
    """Map each requested process name to the pids currently using that comm name.

    Matching is on the exact ``comm`` value (truncated to 15 chars by the kernel)
    or on a prefix of it, which covers wrappers such as ``opencode``.
    """
    wanted = {n.lower(): n for n in names if n}
    found: Dict[str, Set[int]] = {n: set() for n in wanted}
    if not wanted:
        return found
    for pid in all_pids():
        comm = comm_for(pid).lower()
        if not comm:
            continue
        if comm in wanted:
            found[comm].add(pid)
            continue
        for key in wanted:
            if comm.startswith(key) or key.startswith(comm):
                found[key].add(pid)
                break
    return found
