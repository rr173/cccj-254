"""Independent re-check of a certificate against the register and raw readings.

``verify_certificate`` deliberately re-reads the evidence itself (it does not
trust any intermediate from the solver) and checks, for every node of a
certificate:

  * ``0 <= own(v) <= cap(v)`` (capacity evidence);
  * ``total(v) == own(v) + sum(total(c))`` (partition identity);
  * ``total(v)`` is inside every reading interval recorded for ``v`` in the
    period (and there is at least the cited reading, when the node has any);
  * the certificate's claimed usage equals ``sum(weight * own)`` over the
    covered water points.

It returns a list of problems (empty when everything checks out), each of which
quotes enough raw evidence to locate the failing line.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .model import BillingPeriod, Register


def verify_certificate(register: Register, certificate: dict,
                       period: Optional[BillingPeriod] = None) -> List[dict]:
    problems: List[dict] = []

    if period is None:
        key = certificate.get("period_key")
        if key is not None:
            found = [p for p in register.periods if list(p.key()) == list(key)]
        else:
            label = certificate.get("period")
            found = [p for p in register.periods if p.label == label]
        if not found:
            return [{
                "kind": "unknown_period",
                "period": certificate.get("period"),
                "period_key": key,
                "available": [p.label for p in register.periods],
            }]
        period = found[0]
    pkey = period.key()

    nodes = register.nodes
    entries = certificate.get("nodes", [])
    by_node: Dict[int, dict] = {}
    for e in entries:
        v = e["node"]
        if v in by_node:
            problems.append({"kind": "duplicate_node", "node": v})
        by_node[v] = e
        if v not in nodes:
            problems.append({"kind": "unknown_node", "node": v})

    total_covered = 0
    for e in entries:
        v = e["node"]
        if v not in nodes:
            continue
        nd = nodes[v]
        own = e["own"]
        total = e["total"]
        weight = e.get("weight", 0)

        if e.get("parent") != nd.parent:
            problems.append({
                "kind": "parent_mismatch", "node": v,
                "certificate_parent": e.get("parent"), "register_parent": nd.parent,
            })
        if nd.cap is not None and not (0 <= own <= nd.cap):
            problems.append({
                "kind": "own_out_of_capacity", "node": v,
                "own": own, "capacity": nd.cap,
            })
        if own < 0:
            problems.append({"kind": "own_negative", "node": v, "own": own})

        child_sum = 0
        for c in nd.children:
            ce = by_node.get(c)
            if ce is None:
                problems.append({"kind": "missing_descendant", "node": v, "child": c})
            else:
                child_sum += ce["total"]
        if total != own + child_sum:
            problems.append({
                "kind": "partition_violation", "node": v,
                "total": total, "own": own, "child_totals_sum": child_sum,
            })

        readings = register.readings.for_node(pkey, v)
        for (lo, hi, raw) in readings:
            if not (lo <= total <= hi):
                problems.append({
                    "kind": "reading_violation", "node": v,
                    "total": total, "reading": raw,
                })
        total_covered += weight * own

    if certificate.get("usage") != total_covered:
        problems.append({
            "kind": "usage_mismatch",
            "certificate_usage": certificate.get("usage"),
            "recomputed_usage": total_covered,
        })

    # Every node reachable from the certificate root must be present, and no
    # foreign node may be: the account must be complete for its tree.
    root = certificate.get("root")
    if root is not None and root in nodes:
        cert_nodes = set(by_node)
        expected_nodes = set()
        stack = [root]
        while stack:
            v = stack.pop()
            if v in expected_nodes:
                continue
            expected_nodes.add(v)
            stack.extend(nodes[v].children)
        for v in sorted(expected_nodes - cert_nodes):
            problems.append({"kind": "incomplete_account", "node": v})
        for v in sorted(cert_nodes - expected_nodes):
            problems.append({"kind": "foreign_node", "node": v})

    return problems
