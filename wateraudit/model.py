"""Input model: billing periods, the forest register, readings and queries.

The register and the readings are "raw evidence".  Everything in this module
is about *validating* that evidence and turning it into an indexed forest.
Validation here only ever reports

  * INPUT_INCOMPLETE  - broken structure / missing finite total bound
  * INPUT_INVALID     - cycles, unknown ids, non-integer numbers, ...

Usage conflicts (an *exact* reading that no usage can satisfy) are deliberately
*not* raised here: they are per-period facts and are reported by the solver as
``infeasible`` for that period only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class AuditError(Exception):
    """A structured, machine-readable failure.

    ``code`` is one of the constants below.  ``detail`` is a JSON-safe dict with
    enough raw information to re-check the complaint by hand.
    """

    INPUT_INCOMPLETE = "INPUT_INCOMPLETE"
    INPUT_INVALID = "INPUT_INVALID"
    QUERY_INVALID = "QUERY_INVALID"

    def __init__(self, code: str, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {"status": "error", "code": self.code, "message": self.message,
                "detail": self.detail}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"


def _as_int(value: Any, what: str, where: str) -> int:
    """Accept only genuine integers (int, or a string of digits with sign).

    ``bool`` is rejected on purpose; floats are rejected even if integral so
    that a reading of ``1000.0`` never silently becomes a different number.
    """
    if isinstance(value, bool):
        raise AuditError(AuditError.INPUT_INVALID,
                         f"{what} must be an integer, got a boolean", {"where": where})
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 10)
        except ValueError:
            pass
    raise AuditError(AuditError.INPUT_INVALID,
                     f"{what} must be an integer, got {value!r}", {"where": where})


def _as_id(value: Any, what: str, where: str) -> int:
    return _as_int(value, what, where)


# ---------------------------------------------------------------------------
# Billing period
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class BillingPeriod:
    """A named billing period.  Periods are totally ordered by (year, index).

    ``label`` is what appears in reports; it defaults to ``YYYY-NN``.  The
    ordering is used only to *name* periods - never to interpolate between
    them.
    """

    year: int
    index: int
    # The label is presentation only: equality, hashing and ordering all use
    # the (year, index) key, so "JULY@2024-07" and "2024-07" name one period.
    label: str = field(compare=False)
    label_explicit: bool = field(compare=False, default=False)

    def __init__(self, year: int, index: int = 1, label: Optional[str] = None):
        object.__setattr__(self, "year", year)
        object.__setattr__(self, "index", index)
        object.__setattr__(self, "label",
                           label if label is not None else f"{year}-{index:02d}")
        object.__setattr__(self, "label_explicit", label is not None)

    @classmethod
    def parse(cls, token: str) -> "BillingPeriod":
        """Parse ``2024-07`` (no name), ``P2024-07`` (ISO-ish), or ``LABEL@2024-07``.

        The part after ``@`` is always the sort/identity key; the part before it
        is the display label.  Plain ``YYYY-N`` tokens get the default label.
        """
        text = token.strip()
        if not text:
            raise AuditError(AuditError.INPUT_INVALID, "empty period name", {"token": token})
        label = None
        key = text
        if "@" in text:
            label, key = text.split("@", 1)
            label = label.strip()
        if key.startswith("P"):
            key = key[1:]
        parts = key.split("-")
        if len(parts) != 2:
            raise AuditError(AuditError.INPUT_INVALID,
                             f"period must be YYYY-N or LABEL@YYYY-N, got {token!r}",
                             {"token": token})
        year = _as_int(parts[0], "period year", token)
        index = _as_int(parts[1], "period index", token)
        return cls(year, index, label)

    def key(self) -> Tuple[int, int]:
        return (self.year, self.index)


# ---------------------------------------------------------------------------
# Register (the forest of zones)
# ---------------------------------------------------------------------------

@dataclass
class _Node:
    id: int
    parent: int            # -1 for a root
    children: List[int]
    cap: Optional[int]     # own-consumption capacity, None == unbounded
    tree: int              # root index into Register.roots
    depth: int = 0


class Register:
    """An indexed, validated forest plus per-period reading constraints.

    Build it once with :func:`build_register`, then run as many queries as you
    like.  The register is immutable from the solver's point of view.
    """

    def __init__(self, nodes: Dict[int, _Node], roots: List[int],
                 periods: List[BillingPeriod]):
        self.nodes = nodes
        self.roots = roots
        self.periods = periods

    # -- structure helpers ---------------------------------------------------

    def node_ids(self) -> Iterable[int]:
        return self.nodes.keys()

    def free_children(self, v: int) -> List[int]:
        return self.nodes[v].children

    def is_root(self, v: int) -> bool:
        return self.nodes[v].parent < 0


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

@dataclass
class _Reading:
    node: int
    lo: int
    hi: int
    source: Any = None


class Readings:
    """Named reading evidence, indexed by (period, node).

    Accepted input is a list of records::

        {"period": "2024-07", "node": 12, "min": 100, "max": 140, "source": "..."}

    ``min``/``max`` are inclusive; ``max`` defaults to ``min`` (an exact
    reading).  Every record is kept verbatim so conflict reports can quote the
    original numbers.
    """

    def __init__(self, records: Sequence[dict]):
        self.raw: List[dict] = [dict(r) for r in records]
        self.period_names: Dict[Tuple[int, int], BillingPeriod] = {}
        # (period.key, node) -> list of (lo, hi, raw_record)
        self.by_key: Dict[Tuple[Tuple[int, int], int], List[Tuple[int, int, dict]]] = {}
        for pos, rec in enumerate(self.raw):
            where = f"readings[{pos}]"
            if "period" not in rec or "node" not in rec:
                raise AuditError(AuditError.INPUT_INVALID,
                                 "reading needs 'period' and 'node'", {"where": where, "record": rec})
            period = rec["period"]
            if isinstance(period, BillingPeriod):
                parsed = period
            else:
                parsed = BillingPeriod.parse(str(period))
            pkey = parsed.key()
            # The reading's own period name may carry an explicit label
            # ("JULY@2024-07"); remember it so reports use the human name.
            self.period_names.setdefault(pkey, parsed)
            node = _as_id(rec["node"], "reading node", where)
            lo = _as_int(rec.get("min", rec.get("lo")), "reading min", where) \
                if (rec.get("min", rec.get("lo")) is not None) else None
            hi_raw = rec.get("max", rec.get("hi"))
            hi = _as_int(hi_raw, "reading max", where) if hi_raw is not None else lo
            if lo is None:
                raise AuditError(AuditError.INPUT_INVALID,
                                 "reading needs 'min' (or 'lo')", {"where": where, "record": rec})
            if lo > hi:
                raise AuditError(AuditError.INPUT_INVALID,
                                 "reading min > max", {"where": where, "record": rec})
            if lo < 0:
                raise AuditError(AuditError.INPUT_INVALID,
                                 "reading below zero", {"where": where, "record": rec})
            entry = (lo, hi, rec)
            self.by_key.setdefault((pkey, node), []).append(entry)

    def for_node(self, pkey: Tuple[int, int], node: int) -> List[Tuple[int, int, dict]]:
        return self.by_key.get((pkey, node), [])

    def tightest(self, pkey: Tuple[int, int], node: int) -> Optional[Tuple[int, int]]:
        recs = self.for_node(pkey, node)
        if not recs:
            return None
        lo = max(r[0] for r in recs)
        hi = min(r[1] for r in recs)
        return lo, hi


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Query:
    """A named period plus a set of named nodes (metres) covering water points.

    Coverage is the *union of real water points* of the selected subtrees: if
    both an ancestor and a descendant are selected, the descendant's water
    points are counted once.
    """

    period: BillingPeriod
    nodes: Tuple[int, ...]

    def __init__(self, period, nodes: Sequence[int]):
        if not isinstance(period, BillingPeriod):
            period = BillingPeriod.parse(str(period))
        seen = set()
        ordered: List[int] = []
        for n in nodes:
            n = _as_id(n, "query node", "query")
            if n not in seen:
                seen.add(n)
                ordered.append(n)
        object.__setattr__(self, "period", period)
        object.__setattr__(self, "nodes", tuple(ordered))


# ---------------------------------------------------------------------------
# Building / loading
# ---------------------------------------------------------------------------

def _parse_register_obj(obj: Any) -> Tuple[Dict[int, _Node], List[int]]:
    if not isinstance(obj, dict) or "nodes" not in obj:
        raise AuditError(AuditError.INPUT_INCOMPLETE,
                         "register must be an object with a 'nodes' list", {})
    raw_nodes = obj["nodes"]
    if not isinstance(raw_nodes, list):
        raise AuditError(AuditError.INPUT_INVALID, "register 'nodes' must be a list", {})

    nodes: Dict[int, _Node] = {}
    order: List[int] = []          # input order, used for deterministic roots
    for pos, rec in enumerate(raw_nodes):
        where = f"register.nodes[{pos}]"
        if not isinstance(rec, dict):
            raise AuditError(AuditError.INPUT_INVALID, "node record must be an object",
                             {"where": where})
        node_id = _as_id(rec.get("id"), "node id", where)
        if node_id in nodes:
            raise AuditError(AuditError.INPUT_INVALID,
                             f"duplicate node id {node_id}", {"where": where, "node": node_id})
        parent = rec.get("parent")
        parent = _as_id(parent, "node parent", where) if parent is not None else -1
        if "capacity" in rec and rec["capacity"] is not None:
            cap = _as_int(rec["capacity"], "node capacity", where)
            if cap < 0:
                raise AuditError(AuditError.INPUT_INVALID,
                                 "node capacity must be >= 0", {"where": where, "node": node_id})
        else:
            cap = None
        nodes[node_id] = _Node(id=node_id, parent=parent, children=[], cap=cap, tree=-1)
        order.append(node_id)

    # Link parents -> children.  A node that names itself as parent is a
    # broken link (it cannot be part of any tree), and is rejected as
    # incomplete input.  Multi-node cycles are a structural invalidity and are
    # rejected as invalid input; neither is ever treated as a usage conflict.
    for v in order:
        p = nodes[v].parent
        if p < 0:
            continue
        if p == v:
            raise AuditError(AuditError.INPUT_INCOMPLETE,
                             f"node {v} is its own parent (broken link)",
                             {"node": v})
        if p not in nodes:
            raise AuditError(AuditError.INPUT_INCOMPLETE,
                             f"node {v} points at missing parent {p} (broken link)",
                             {"node": v, "parent": p})
        nodes[p].children.append(v)

    # Reject cycles before any traversal (iterative colour DFS; recursion depth
    # must not depend on tree depth - registers can be 100k nodes deep).
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {v: WHITE for v in order}
    for start in order:
        if colour[start] != WHITE:
            continue
        stack = [(start, 0)]
        colour[start] = GREY
        while stack:
            v, i = stack[-1]
            ch = nodes[v].children
            if i < len(ch):
                stack[-1] = (v, i + 1)
                w = ch[i]
                if colour[w] == GREY:
                    raise AuditError(AuditError.INPUT_INVALID,
                                     "register contains a cycle",
                                     {"cycle_through": w, "at": v})
                if colour[w] == WHITE:
                    colour[w] = GREY
                    stack.append((w, 0))
            else:
                colour[v] = BLACK
                stack.pop()

    roots = [v for v in order if nodes[v].parent < 0]
    if not roots:
        raise AuditError(AuditError.INPUT_INVALID, "register has no root", {})
    return nodes, roots


def _assign_tree_ids(nodes: Dict[int, _Node], roots: List[int]) -> None:
    for root_idx, root in enumerate(roots):
        stack = [(root, 0)]
        while stack:
            v, depth = stack.pop()
            nodes[v].tree = root_idx
            nodes[v].depth = depth
            for c in nodes[v].children:
                stack.append((c, depth + 1))


def build_register(register_obj: dict,
                   readings: Sequence[dict] = (),
                   periods: Optional[Sequence[Any]] = None) -> Register:
    """Validate a register object plus readings; return an indexed Register.

    ``periods`` may name additional periods (e.g. ones that only appear in the
    query).  Periods mentioned by readings are collected automatically.
    """
    nodes, roots = _parse_register_obj(register_obj)

    period_map: Dict[Tuple[int, int], BillingPeriod] = {}

    def register_period(token: Any) -> BillingPeriod:
        if isinstance(token, BillingPeriod):
            p = token
        else:
            p = BillingPeriod.parse(str(token))
        prev = period_map.get(p.key())
        # Prefer an explicit label over the default: "JULY@2024-07" and
        # "2024-07" are the same period, and the human name wins for display.
        if prev is None or (p.label_explicit and not prev.label_explicit):
            period_map[p.key()] = p
        return period_map[p.key()]

    for token in periods or ():
        register_period(token)

    rds = Readings(readings)
    # A reading for a zone that is not in the register is dangling evidence;
    # silently dropping it could hide a typo'd id and understate a bill, so it
    # is reported as an input problem instead.
    orphan = sorted({node for (_pkey, node) in rds.by_key if node not in nodes})
    if orphan:
        missing = set(orphan)
        samples = [dict(r) for r in rds.raw
                   if _as_int(r["node"], "reading node", "reading") in missing][:5]
        raise AuditError(AuditError.INPUT_INCOMPLETE,
                         "readings reference zones not present in the register",
                         {"missing_nodes": orphan[:20], "count": len(orphan),
                          "sample_readings": samples})
    # Fold in labels carried by the readings themselves.
    for pkey, named_period in rds.period_names.items():
        if pkey not in period_map or (named_period.label_explicit
                                      and not period_map[pkey].label_explicit):
            period_map[pkey] = named_period
    # Periods referenced by readings but not declared must exist for lookup.
    for (pkey, _node) in rds.by_key:
        if pkey not in period_map:
            period_map[pkey] = BillingPeriod(pkey[0], pkey[1])

    _assign_tree_ids(nodes, roots)

    reg = Register(nodes, roots, sorted(period_map.values(), key=lambda p: p.key()))
    reg.readings = rds
    return reg


def load_register(register_path: str, readings_path: str,
                  periods: Optional[Sequence[Any]] = None) -> Register:
    """Load a register JSON file and a readings JSON file from disk.

    Both files are opened read-only.  This is the entry point used by the CLI
    so a run can be reproduced from the local files alone.
    """
    with open(register_path, "r", encoding="utf-8") as fh:
        register_obj = json.load(fh)
    with open(readings_path, "r", encoding="utf-8") as fh:
        readings_obj = json.load(fh)
    if isinstance(readings_obj, dict):
        readings_obj = readings_obj.get("readings", [])
    return build_register(register_obj, readings_obj, periods)
