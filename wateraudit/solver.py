"""Per-tree linear solver, endpoint DP, and reconciliation certificates.

Model for one tree and one billing period
=========================================

For every zone ``v``::

    own(v)    integer, 0 <= own(v) <= cap(v)          (cap may be unbounded)
    total(v)  = own(v) + sum_{c child of v} total(c)
    total(v)  in [lo(v), hi(v)]

``[lo(v), hi(v)]`` is the intersection of *all* readings recorded for ``v`` in
this period.  A zone with no reading has ``[0, +inf)`` - an un-read zone is
never assumed to be zero.  A zone whose readings do not intersect makes the
period infeasible (a usage conflict, not invalid input).

For each node we compute ``m(v)``/``M(v)``: the minimum / maximum reachable
``total(v)`` over *feasible* assignments of the whole subtree.  Two interval
properties make the DP exact and the greedy certificate pass safe:

* the reachable ``total(v)`` values form the integer interval
  ``[m(v), M(v)]`` (a sum of integer intervals with an own-usage interval is an
  interval);
* for a *fixed* root total ``Q``, the reachable ``total(v)`` values at every
  node ``v`` also form a contiguous interval.  This is what guarantees the
  top-down pass never strands quota part-way (any residual ``R`` at a node can
  be absorbed by a child's remaining slack without violating its bounds).

Both properties are checked against brute force in the test-suite.

Query objective
===============

A query names zones ``S``.  ``w(v) = 1`` iff ``v`` or one of its ancestors is
in ``S``.  The covered real water points are exactly ``sum_v w(v) * own(v)`` -
an ancestor covers its own point plus every descendant's, and overlaps are
counted once.  Because ``w`` is *constant* on each selected subtree, the
minimum and maximum of this linear form are obtained by two local DPs whose
state at each node is a single interval.  No usage allocation is ever
enumerated, and the cost is O(V + E + #readings), independent of usage
magnitudes and interval widths.

DP (validated exhaustively against brute force in tests/test_bruteforce.py)
--------------------------------------------------------------------------

Minimum side:
    m0(v)    = sum_c m(c)
    free(v)  = (cap(v) if w(v) == 0 else 0) + sum_c Z(c)
    gain(v)  = m(v) - m0(v)
    alpha(v) = sum_c alpha(c) + max(0, gain(v) - free(v))
    Z(v)     = min(M(v) - m(v), max(0, free(v) - gain(v)))
    usage_min = alpha(root)

Maximum side:
    free(v)  = (cap(v) if w(v) == 1 else 0) + sum_c G(c)
    alpha'(v)= sum_c alpha'(c) + min(gain(v), free(v))
    G(v)     = min(M(v) - m(v), max(0, free(v) - gain(v)))
    usage_max = alpha'(root) + G(root)

A node with ``m(v) > M(v)`` makes the whole tree infeasible for the period.
Conflicts are localised to the deepest node on each branch where the local
arithmetic itself contradicts (the true cause); its ancestors are merely
consequences.  The report quotes those nodes' raw readings and the partition
arithmetic.

Certificates
============

For each endpoint a full assignment ``own``/``total`` is constructed by one
top-down greedy pass with the root total fixed at ``m(root)`` (minimum) or
``M(root)`` (maximum), distributing the quota through the same local slack
intervals the DP used.  The resulting account can be checked line by line
against the register and raw readings - see :mod:`wateraudit.verify`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Internal sentinel for "+infinity" evidence/capacity.  Arithmetic saturates at
# INF, and any value at or above ``UNBOUNDED`` is reported as unbounded rather
# than as a number.  Real evidence is described as at most ~1e12 per reading
# with at most ~1e5 nodes, i.e. < 1e18; these thresholds are far above that.
INF = 1 << 80
UNBOUNDED = 1 << 79


def _sat_add(a: int, b: int) -> int:
    s = a + b
    return INF if s >= INF else s


@dataclass
class NodeBound:
    node: int
    cap: Optional[int]
    reading_lo: int
    reading_hi: int
    m: int
    M: int
    alpha_min: int
    z_slack: int
    alpha_max: int
    g_slack: int
    weight: int

    @property
    def has_reading(self) -> bool:
        return self.reading_lo != 0 or self.reading_hi < INF

    @property
    def reading_interval(self) -> Optional[Tuple[int, Optional[int]]]:
        if self.reading_lo == 0 and self.reading_hi >= INF:
            return None
        return (self.reading_lo, None if self.reading_hi >= INF else self.reading_hi)


@dataclass
class TreeResult:
    period: object
    root: int
    members: List[int]
    selected: List[int]
    weight: Dict[int, int]
    status: str                       # "feasible" | "infeasible"
    usage_min: Optional[int] = None
    usage_max: Optional[int] = None
    bounds: Dict[int, NodeBound] = field(default_factory=dict)
    conflicts: List[dict] = field(default_factory=list)
    _core: dict = field(default_factory=dict, repr=False)

    @property
    def feasible(self) -> bool:
        return self.status == "feasible"

    @property
    def invoiceable(self) -> bool:
        """Only an endpoint-identical (unique integer usage) result may be invoiced."""
        return self.feasible and self.usage_min == self.usage_max


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def tree_members(nodes, root: int) -> List[int]:
    """Pre-order (parents before children) listing of one tree. Iterative."""
    order: List[int] = []
    stack = [root]
    while stack:
        v = stack.pop()
        order.append(v)
        stack.extend(reversed(nodes[v].children))
    return order


def _weights(order: List[int], nodes, selected: frozenset) -> Dict[int, int]:
    w: Dict[int, int] = {}
    for v in order:
        p = nodes[v].parent
        w[v] = 1 if (v in selected or (p >= 0 and w[p])) else 0
    return w


# ---------------------------------------------------------------------------
# solver
# ---------------------------------------------------------------------------

def solve_tree(register, root: int, period, selected: frozenset) -> TreeResult:
    """Solve one tree for one period with the given selected nodes."""
    pkey = period.key() if hasattr(period, "key") else period
    nodes = register.nodes
    readings = register.readings

    order = tree_members(nodes, root)      # parents first
    post = list(reversed(order))           # children first

    # ---- reading evidence: intersection of every reading of this node ------
    lo: Dict[int, int] = {}
    hi: Dict[int, int] = {}
    evidence: Dict[int, List[dict]] = {}
    disagreeing: List[int] = []
    for v in order:
        recs = readings.for_node(pkey, v)
        evidence[v] = [dict(r[2]) for r in recs]
        if not recs:
            lo[v], hi[v] = 0, INF
            continue
        l = max(r[0] for r in recs)
        h = min(r[1] for r in recs)
        if l > h:
            disagreeing.append(v)
            l, h = h, l          # keep raw bounds for the report; flagged below
        lo[v], hi[v] = l, h

    # ---- m/M: reachable total range per subtree ---------------------------
    m: Dict[int, int] = {}
    M: Dict[int, int] = {}
    for v in post:
        cap = nodes[v].cap if nodes[v].cap is not None else INF
        m0 = 0
        M0 = 0
        for c in nodes[v].children:
            m0 = _sat_add(m0, m[c])
            M0 = _sat_add(M0, M[c])
        m[v] = lo[v] if lo[v] > m0 else m0
        hi_cap = _sat_add(cap, M0)
        M[v] = hi[v] if hi[v] < hi_cap else hi_cap

    weight = _weights(order, nodes, selected)

    # ---- min-side DP -------------------------------------------------------
    alpha_min: Dict[int, int] = {}
    z_slack: Dict[int, int] = {}
    for v in post:
        cap = nodes[v].cap if nodes[v].cap is not None else INF
        m0 = 0
        beta = 0
        free = 0 if weight[v] else cap
        for c in nodes[v].children:
            m0 = _sat_add(m0, m[c])
            beta = _sat_add(beta, alpha_min[c])
            free = _sat_add(free, z_slack[c])
        gain = m[v] - m0
        if gain < 0:
            gain = 0
        excess = gain - free
        alpha_min[v] = _sat_add(beta, excess if excess > 0 else 0)
        slack = free - gain
        if slack < 0:
            slack = 0
        width = M[v] - m[v]
        z_slack[v] = min(slack, width) if width > 0 else 0

    # ---- max-side DP -------------------------------------------------------
    alpha_max: Dict[int, int] = {}
    g_slack: Dict[int, int] = {}
    for v in post:
        cap = nodes[v].cap if nodes[v].cap is not None else INF
        m0 = 0
        beta = 0
        free = cap if weight[v] else 0
        for c in nodes[v].children:
            m0 = _sat_add(m0, m[c])
            beta = _sat_add(beta, alpha_max[c])
            free = _sat_add(free, g_slack[c])
        gain = m[v] - m0
        if gain < 0:
            gain = 0
        alpha_max[v] = _sat_add(beta, gain if gain < free else free)
        slack = free - gain
        if slack < 0:
            slack = 0
        width = M[v] - m[v]
        g_slack[v] = min(slack, width) if width > 0 else 0

    bounds: Dict[int, NodeBound] = {}
    for v in order:
        cap = nodes[v].cap
        bounds[v] = NodeBound(
            node=v, cap=cap,
            reading_lo=lo[v], reading_hi=hi[v],
            m=m[v], M=M[v],
            alpha_min=alpha_min[v], z_slack=z_slack[v],
            alpha_max=alpha_max[v], g_slack=g_slack[v],
            weight=weight[v],
        )

    # ---- feasibility -------------------------------------------------------
    local_bad = [v for v in order if m[v] > M[v]]
    status = "feasible" if not disagreeing and not local_bad else "infeasible"
    usage_min: Optional[int] = None
    usage_max: Optional[int] = None
    conflicts: List[dict] = []

    if status == "infeasible":
        for v in disagreeing:
            conflicts.append({
                "kind": "readings_disagree",
                "node": v,
                "readings": evidence[v],
                "tightest_intersection": None,
                "explanation": (
                    "all readings of one zone for the same period must hold; "
                    "these intervals do not intersect"
                ),
            })
        bad = set(local_bad)
        for v in order:
            if v not in bad or v in disagreeing:
                continue
            # Report only the deepest bad node on each branch: its own
            # arithmetic contradicts while every child subtree is satisfiable.
            if any(c in bad for c in nodes[v].children):
                continue
            conflicts.append({
                "kind": "subtree_infeasible",
                "node": v,
                "node_readings": evidence[v],
                "own_capacity": nodes[v].cap,
                "children_required_min": {c: m[c] for c in nodes[v].children},
                "children_required_max": {c: M[c] for c in nodes[v].children},
                "required_interval": [m[v], M[v]],
                "explanation": (
                    f"total({v}) must be >= {m[v]} (readings / children) and "
                    f"<= {M[v]} (capacity + readings); no integer satisfies both"
                ),
            })
    else:
        usage_min = alpha_min[root]
        usage_max = _sat_add(alpha_max[root], g_slack[root])

    return TreeResult(
        period=period, root=root, members=order, selected=sorted(selected),
        weight=weight, status=status,
        usage_min=usage_min, usage_max=usage_max,
        bounds=bounds, conflicts=conflicts,
        _core={
            "order": order, "m": m, "M": M,
            "z": z_slack, "g": g_slack,
            "lo": lo, "hi": hi, "evidence": evidence,
        },
    )


# ---------------------------------------------------------------------------
# certificates
# ---------------------------------------------------------------------------

def build_certificate(register, result: TreeResult, endpoint: str) -> dict:
    """Construct the complete per-zone account attaining one endpoint.

    ``endpoint`` is ``"min"`` or ``"max"``.  The account lists, for every zone
    of the tree, its own usage, its zone total, its capacity, the applied
    reading interval and the raw readings - enough to re-check every line.
    """
    if not result.feasible:
        raise ValueError("cannot build a certificate for an infeasible tree")
    if endpoint not in ("min", "max"):
        raise ValueError(f"unknown endpoint {endpoint!r}")

    core = result._core
    order = core["order"]
    m, M, z, g = core["m"], core["M"], core["z"], core["g"]
    nodes = register.nodes
    weight = result.weight
    evidence = core["evidence"]

    # Fix the root total at the extreme that attains the endpoint.  The DP's
    # objective already includes the root's own usage when the root is covered,
    # so this quota is exactly what makes the greedy pass land on the endpoint.
    Q = m[result.root] if endpoint == "min" else M[result.root]
    slack = z if endpoint == "min" else g

    q: Dict[int, int] = {result.root: Q}
    own: Dict[int, int] = {}
    for v in order:
        ch = nodes[v].children
        cap = nodes[v].cap if nodes[v].cap is not None else INF
        m0 = 0
        for c in ch:
            m0 = _sat_add(m0, m[c])
        remaining = q[v] - m0
        if remaining < 0:
            raise AssertionError(f"certificate underflow at node {v}")

        ev = 0
        extra: Dict[int, int] = {c: 0 for c in ch}
        # Phase 1: the node's own usage gets quota first if it is *not*
        # covered by the query; if it *is* covered, child subtrees get their
        # free slack first so that own usage lands at its extreme.
        take_own_first = (weight[v] == 1) == (endpoint == "max")
        if take_own_first:
            take = cap if cap < remaining else remaining
            ev += take
            remaining -= take
        for c in ch:
            take = slack[c] if slack[c] < remaining else remaining
            extra[c] += take
            remaining -= take
        if not take_own_first:
            take = cap if cap < remaining else remaining
            ev += take
            remaining -= take
        # Phase 2: hand out the remainder to child subtrees up to their width.
        for c in ch:
            room = M[c] - m[c] - extra[c]
            if room < 0:
                room = 0
            take = room if room < remaining else remaining
            extra[c] += take
            remaining -= take
        if remaining != 0:
            raise AssertionError(
                f"certificate construction failed at node {v}: {remaining} unassigned"
            )
        own[v] = ev
        for c in ch:
            q[c] = m[c] + extra[c]

    entries = []
    total_covered = 0
    for v in order:
        entries.append({
            "node": v,
            "parent": nodes[v].parent,
            "weight": weight[v],
            "own": own[v],
            "total": q[v],
            "capacity": nodes[v].cap,
            "reading": [core["lo"][v], None if core["hi"][v] >= INF else core["hi"][v]],
            "readings": evidence.get(v, []),
        })
        total_covered += weight[v] * own[v]

    cert = {
        "endpoint": endpoint,
        "root": result.root,
        "period": result.period.label if hasattr(result.period, "label") else str(result.period),
        "period_key": list(result.period.key()) if hasattr(result.period, "key") else None,
        "usage": total_covered,
        "nodes": entries,
    }
    expected = result.usage_min if endpoint == "min" else result.usage_max
    if total_covered != expected:
        raise AssertionError(
            f"certificate usage {total_covered} != {endpoint} endpoint {expected}"
        )
    return cert
