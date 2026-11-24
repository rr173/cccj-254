"""Rendering audit results as stable JSON or human-readable text.

JSON is the machine format used by the CLI and is safe to store; it contains
only integers, strings, booleans and nulls (no floats), so large usage values
survive a round trip exactly.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

from .audit import AuditResult, QueryReport


def _json_default(obj: Any):
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    raise TypeError(f"not JSON serialisable: {type(obj)!r}")


def render_json(result: AuditResult, indent: Optional[int] = 2) -> str:
    return json.dumps(result.as_dict(), ensure_ascii=False, indent=indent,
                      default=_json_default, sort_keys=False)


def _render_query(q: QueryReport, lines: List[str]) -> None:
    name = getattr(q.query, "name", None)
    head = f"Query {name + ' ' if name else ''}[{q.query.period.label}] " \
           f"nodes={list(q.query.nodes)}"
    lines.append(head)
    if q.status == "feasible":
        lines.append(f"  usage_min = {q.usage_min}")
        lines.append(f"  usage_max = {q.usage_max}")
        lines.append(f"  invoiceable (both endpoints equal) = "
                     f"{'YES' if q.invoiceable else 'no'}")
        for t in q.trees:
            lines.append(f"  tree root {t['tree_root']}: selected={t['selected']} "
                         f"range=[{t['usage_min']}, {t['usage_max']}]")
            for ep in ("min", "max"):
                cert = t["certificates"][ep]
                lines.append(f"    certificate({ep}) usage={cert['usage']} "
                             f"nodes={len(cert['nodes'])}")
                for e in cert["nodes"]:
                    cap = "inf" if e["capacity"] is None else e["capacity"]
                    rd = e["reading"]
                    rd_txt = f"[{rd[0]}, inf)" if rd[1] is None else f"[{rd[0]}, {rd[1]}]"
                    star = "*" if e["weight"] else " "
                    lines.append(
                        f"      {star} node {e['node']:>7} own={e['own']:>14} "
                        f"total={e['total']:>18} cap={cap:>12} reading={rd_txt}"
                    )
    elif q.status == "infeasible":
        lines.append("  status = INFEASIBLE (usage conflict - no interval produced)")
        for c in q.conflicts:
            lines.append(f"  tree root {c['tree_root']}:")
            for item in c["conflicts"]:
                lines.append(f"    - {json.dumps(item, ensure_ascii=False)}")
    else:
        lines.append(f"  status = INPUT_INCOMPLETE - no interval produced")
        for e in q.errors:
            lines.append(f"    - {json.dumps(e, ensure_ascii=False)}")
    lines.append("")


def render_text(result: AuditResult) -> str:
    lines: List[str] = []
    lines.append(f"periods: {', '.join(p.label for p in result.periods)}")
    if result.standalone:
        lines.append("standalone per-tree period check:")
        for row in result.standalone:
            extra = ""
            if row["status"] == "feasible":
                extra = f" range=[{row['usage_min']}, {row['usage_max']}]"
            elif row["status"] == "infeasible":
                extra = f" conflicts={len(row['conflicts'])}"
            lines.append(f"  [{row['period']}] tree {row['tree']} root {row['root']}: "
                         f"{row['status']}{extra}")
        lines.append("")
    for q in result.queries:
        _render_query(q, lines)
    return "\n".join(lines).rstrip() + "\n"
