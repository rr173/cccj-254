"""Shared test helpers: randomized forests and a brute-force oracle."""

from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wateraudit import build_register  # noqa: E402

PERIOD = "2024-01"


def random_tree(rnd: random.Random, n: int, mode: Optional[str] = None
                ) -> Tuple[Dict[int, List[int]], Dict[int, Optional[int]]]:
    """Return (children, parent) dicts for a random tree on 0..n-1."""
    children: Dict[int, List[int]] = {i: [] for i in range(n)}
    parent: Dict[int, Optional[int]] = {0: None}
    mode = mode or rnd.choice(["chain", "star", "mixed", "binary"])
    for i in range(1, n):
        if mode == "chain":
            p = i - 1
        elif mode == "star":
            p = 0
        elif mode == "binary":
            p = (i - 1) // 2
        else:
            p = rnd.randrange(i)
        parent[i] = p
        children[p].append(i)
    return children, parent


def preorder(children: Dict[int, List[int]], root: int = 0) -> List[int]:
    order: List[int] = []
    stack = [root]
    while stack:
        v = stack.pop()
        order.append(v)
        stack.extend(reversed(children[v]))
    return order


def brute_force(children, parent, cap, lo, hi, root, selected,
                max_enum: int = 60_000, max_cap: int = 12):
    """Exact min/max covered usage by enumerating own-usage vectors.

    Returns (min, max) or (None, None) when infeasible.  Returns ("skip", None)
    if the enumeration space is too large (never silently wrong).
    """
    order = preorder(children, root)
    weights: Dict[int, int] = {}
    for v in order:
        p = parent[v]
        weights[v] = 1 if (v in selected or (p is not None and weights[p])) else 0
    ranges = []
    space = 1
    for v in order:
        top = cap[v] if cap[v] is not None else max_cap
        if top > max_cap:
            top = max_cap
        ranges.append(range(top + 1))
        space *= (top + 1)
    if space > max_enum:
        return "skip", None

    vmin = vmax = None
    for combo in itertools.product(*ranges):
        own = dict(zip(order, combo))
        totals: Dict[int, int] = {}
        ok = True
        for v in reversed(order):
            s = own[v] + sum(totals[c] for c in children[v])
            if s < lo[v] or s > hi[v]:
                ok = False
                break
            totals[v] = s
        if not ok:
            continue
        z = sum(weights[v] * own[v] for v in order)
        vmin = z if vmin is None else min(vmin, z)
        vmax = z if vmax is None else max(vmax, z)
    return vmin, vmax


def make_forest(spec: dict):
    """Build a Register from a spec.

    spec = {
      "trees": [ {"parent": {...}, "cap": {...}, "readings": {...}}, ... ],
    }
    Node ids in later trees are offset automatically; readings use local ids.
    """
    nodes = []
    readings = []
    for ti, tree in enumerate(spec["trees"]):
        offset = spec.get("offsets", [None] * len(spec["trees"]))[ti]
        parent = tree["parent"]
        for local, p in parent.items():
            gid = local if offset is None else local + offset
            gp = None if p is None else (p if offset is None else p + offset)
            nodes.append({"id": gid, "parent": gp,
                          "capacity": tree["cap"].get(local)})
        for local, rng in tree.get("readings", {}).items():
            gid = local if offset is None else local + offset
            if rng is None:
                continue
            a, b = rng
            readings.append({"period": PERIOD, "node": gid, "min": a, "max": b})
    return build_register({"nodes": nodes}, readings)
