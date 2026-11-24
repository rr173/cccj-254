"""Whole-forest audit: per-tree period checks, cross-tree queries, witnesses.

Status vocabulary used everywhere in this package
--------------------------------------------------
Reportable (a query answer)
    ``feasible``   - an interval was computed (both endpoints).
``infeasible``
    A *usage conflict*: the evidence for a tree in this period admits no
    integer assignment.  No interval is produced for that tree/period; the
    conflict carries the raw readings and the local partition arithmetic
    needed to re-derive the contradiction.
Input problems (never a usage conflict)
    ``input_incomplete`` / ``input_invalid`` - broken parent links, cycles,
    unknown ids, malformed numbers, or a tree total with no finite upper bound
    for the period.  These are raised as :class:`AuditError` at load time for
    structural problems, and reported as ``input_incomplete`` per (tree,
    period) when the total's finite upper bound is missing.

Query semantics
---------------
A query names a billing period and a set of zones.  The covered water points
are the *union* of the selected subtrees' own water points, i.e.
``sum_v w(v) * own(v)`` with ``w(v) = 1`` iff ``v`` or an ancestor of ``v`` is
selected.  Selecting an ancestor and a descendant therefore counts the
overlap once.  Queries may span trees; because trees are independent, the
interval of the union is the sum of per-tree intervals and the per-tree
certificates together form the complete account.

Cost: O((V + E + R)) per (tree, period) solved, cached by (period, tree).
Nothing depends on the numeric magnitude of evidence or interval widths, and
no allocation is enumerated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .model import AuditError, BillingPeriod, Query, Register
from .solver import INF, UNBOUNDED, TreeResult, build_certificate, solve_tree


@dataclass
class QueryReport:
    query: Query
    status: str = "feasible"
    usage_min: Optional[int] = None
    usage_max: Optional[int] = None
    invoiceable: bool = False
    trees: List[dict] = field(default_factory=list)
    conflicts: List[dict] = field(default_factory=list)
    errors: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": getattr(self.query, "name", None),
            "period": self.query.period.label,
            "period_key": list(self.query.period.key()),
            "selected": list(self.query.nodes),
            "status": self.status,
            "usage_min": self.usage_min,
            "usage_max": self.usage_max,
            "invoiceable": self.invoiceable,
            "trees": self.trees,
            "conflicts": self.conflicts,
            "errors": self.errors,
        }


@dataclass
class AuditResult:
    register: Register
    periods: List[BillingPeriod]
    standalone: List[dict]
    queries: List[QueryReport] = field(default_factory=list)

    @property
    def status(self) -> str:
        # A query is reportable per period; one bad period does not poison the
        # others.  The overall verdict is only "not_reportable" when *no*
        # query returned an interval.
        if self.queries and all(q.status == "feasible" for q in self.queries):
            return "ok"
        if any(q.status == "feasible" for q in self.queries):
            return "partial"
        return "not_reportable"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "periods": [p.label for p in self.periods],
            "standalone": self.standalone,
            "queries": [q.as_dict() for q in self.queries],
        }


def root_bound_problem(register: Register, root: int, period_key) -> Optional[dict]:
    """A tree total must have *reading* evidence of a finite upper bound.

    This is a hard input requirement per period, distinct from a usage
    conflict: without a finite upper bound on the total meter the interval can
    be unbounded, and the audit must say "input incomplete" rather than invent
    a number.  A capacity bound alone does not qualify - the evidence has to be
    a reading of the total.
    """
    bound = register.readings.tightest(period_key, root)
    if bound is not None and bound[1] < INF:
        return None
    recs = [dict(r[2]) for r in register.readings.for_node(period_key, root)]
    return {
        "root": root,
        "problem": "tree total has no reading evidence of a finite upper bound "
                   "for this period",
        "node_readings": recs,
    }


class _Cache:
    __slots__ = ("data",)

    def __init__(self):
        self.data: Dict[Tuple[Tuple[int, int], int], TreeResult] = {}

    def get(self, register, root, period):
        k = (period.key(), root)
        res = self.data.get(k)
        if res is None:
            res = solve_tree(register, root, period, frozenset())
            self.data[k] = res
        return res


def audit(register: Register, queries: Sequence[Query],
          standalone: bool = True) -> AuditResult:
    """Run the queries and (optionally) a per-tree check for each named period."""
    nodes = register.nodes
    for q in queries:
        for v in q.nodes:
            if v not in nodes:
                raise AuditError(AuditError.QUERY_INVALID,
                                 f"query names unknown node {v}", {"node": v})

    node_root: Dict[int, int] = {v: register.roots[nodes[v].tree] for v in nodes}
    cache = _Cache()
    # Canonical display name for each period key, taken from the register.
    canonical: Dict[Tuple[int, int], BillingPeriod] = {p.key(): p for p in register.periods}

    named: Dict[Tuple[int, int], BillingPeriod] = {}
    for q in queries:
        named.setdefault(q.period.key(), q.period)

    standalone_rows: List[dict] = []
    if standalone:
        for period in sorted(named.values(), key=lambda p: p.key()):
            for ti, root in enumerate(register.roots):
                problem = root_bound_problem(register, root, period.key())
                if problem is not None:
                    standalone_rows.append({
                        "period": period.label, "tree": ti, "root": root,
                        "status": "input_incomplete", "detail": problem,
                    })
                    continue
                res = cache.get(register, root, period)
                if res.status != "feasible":
                    standalone_rows.append({
                        "period": period.label, "tree": ti, "root": root,
                        "status": "infeasible",
                        "conflicts": _conflicts(register, res),
                    })
                else:
                    standalone_rows.append({
                        "period": period.label, "tree": ti, "root": root,
                        "status": "feasible",
                        "usage_min": res.usage_min, "usage_max": res.usage_max,
                    })

    result = AuditResult(register=register,
                         periods=sorted(named.values(), key=lambda p: p.key()),
                         standalone=standalone_rows)
    for q in queries:
        result.queries.append(
            _run_query(register, q, node_root, cache, canonical))
    return result


def _conflicts(register: Register, res: TreeResult) -> List[dict]:
    nodes = register.nodes
    out = []
    for c in res.conflicts:
        c = dict(c)
        if c["kind"] == "subtree_infeasible":
            v = c["node"]
            c["children_evidence"] = [
                {
                    "node": ch,
                    "capacity": nodes[ch].cap,
                    "readings": _node_evidence(res, ch),
                    "reachable_total": [res._core["m"][ch], res._core["M"][ch]],
                }
                for ch in nodes[v].children
            ]
        out.append(c)
    return out


def _node_evidence(res: TreeResult, node: int) -> List[dict]:
    return res._core["evidence"].get(node, [])


def _run_query(register: Register, query: Query,
               node_root: Dict[int, int], cache: _Cache,
               canonical: Dict[Tuple[int, int], BillingPeriod]) -> QueryReport:
    # Report with the register's canonical label for this period key (e.g.
    # "JULY" if the readings named it that), while evidence lookup is by key.
    # The query's display name, if it set one, is preserved.
    period = canonical.get(query.period.key(), query.period)
    name = getattr(query, "name", None)
    query = Query(period, query.nodes)
    if name is not None:
        object.__setattr__(query, "name", name)
    by_tree: Dict[int, List[int]] = {}
    for v in query.nodes:
        by_tree.setdefault(node_root[v], []).append(v)

    rep = QueryReport(query=query)
    total_min = 0
    total_max = 0
    any_conflict = False
    any_error = False

    for root, sel in sorted(by_tree.items()):
        problem = root_bound_problem(register, root, period.key())
        if problem is not None:
            rep.errors.append({"tree_root": root, **problem})
            any_error = True
            continue
        res = solve_tree(register, root, period, frozenset(sel))
        if res.status != "feasible":
            rep.conflicts.append({
                "tree_root": root,
                "selected": sorted(sel),
                "conflicts": _conflicts(register, res),
            })
            any_conflict = True
            rep.trees.append({
                "tree_root": root, "status": "infeasible",
                "selected": sorted(sel), "usage_min": None, "usage_max": None,
            })
            continue
        # An unbounded endpoint cannot be reported as a number.  This cannot
        # happen when the root has finite reading evidence, but the guard
        # makes the guarantee explicit rather than implicit.
        if res.usage_min is None or res.usage_max is None \
                or res.usage_min >= UNBOUNDED or res.usage_max >= UNBOUNDED:
            rep.errors.append({
                "tree_root": root,
                "problem": "finite bounds imply an unbounded usage endpoint",
                "usage_min": res.usage_min, "usage_max": res.usage_max,
            })
            any_error = True
            continue
        total_min += res.usage_min
        total_max += res.usage_max
        rep.trees.append({
            "tree_root": root,
            "status": "feasible",
            "selected": sorted(sel),
            "usage_min": res.usage_min,
            "usage_max": res.usage_max,
            "certificates": {
                "min": build_certificate(register, res, "min"),
                "max": build_certificate(register, res, "max"),
            },
        })

    if any_conflict:
        rep.status = "infeasible"
        rep.usage_min = rep.usage_max = None
    elif any_error:
        rep.status = "input_incomplete"
        rep.usage_min = rep.usage_max = None
    else:
        rep.status = "feasible"
        rep.usage_min = total_min
        rep.usage_max = total_max
        rep.invoiceable = total_min == total_max
    return rep
