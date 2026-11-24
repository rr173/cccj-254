"""Command-line entry point: reproducible audits from local files.

Examples
--------

    python -m wateraudit audit \\
        --register examples/simple/register.json \\
        --readings examples/simple/readings.json \\
        --period 2024-07 --nodes 3,4 \\
        --json

Exit codes: 0 when every query is feasible (reportable), 2 when a query is
infeasible or has an input problem, 1 for a load/validation error.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .audit import audit
from .model import AuditError, BillingPeriod, Query, load_register
from .report import render_json, render_text


def _parse_nodes(text: str) -> List[int]:
    """Parse ``3,4,10-12`` into ids.  Negative ids are allowed as bare ids."""
    out: List[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("-"):
            out.append(int(part))          # negative id, not a range
            continue
        if part.count("-") == 1:
            a, b = part.split("-")
            lo, hi = int(a), int(b)
            if lo > hi:
                raise AuditError(AuditError.QUERY_INVALID,
                                 f"empty node range {part!r}", {"range": part})
            out.extend(range(lo, hi + 1))
        else:
            out.append(int(part))
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wateraudit",
                                description="Offline water-usage reconciliation")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("audit", help="audit one period and a set of zones")
    a.add_argument("--register", required=True, help="register JSON path")
    a.add_argument("--readings", required=True, help="readings JSON path")
    a.add_argument("--period", required=True, action="append",
                   help="period, e.g. 2024-07 or label@2024-07 (repeatable)")
    a.add_argument("--nodes", required=True, action="append",
                   help="comma/range list of zone ids, e.g. 3,4,10-12 (repeatable)")
    a.add_argument("--name", action="append", default=None,
                   help="optional query name (paired with --period by position)")
    a.add_argument("--no-standalone", action="store_true",
                   help="skip the per-tree period feasibility table")
    a.add_argument("--json", action="store_true", help="emit JSON instead of text")
    a.add_argument("--indent", type=int, default=2, help="JSON indent (default 2)")
    a.add_argument("--out", help="write output to this path instead of stdout")
    return p


def _run_audit(args) -> int:
    register = load_register(args.register, args.readings)

    periods = [BillingPeriod.parse(t) for t in args.period]
    node_lists = [_parse_nodes(t) for t in args.nodes]
    if len(node_lists) == 1:
        node_lists = node_lists * len(periods)
    if len(node_lists) != len(periods):
        raise AuditError(AuditError.QUERY_INVALID,
                         "each --period must have a matching --nodes",
                         {"periods": len(periods), "node_lists": len(node_lists)})
    names: List[Optional[str]] = list(args.name or [])
    while len(names) < len(periods):
        names.append(None)

    queries = []
    for period, nodes, name in zip(periods, node_lists, names):
        q = Query(period, nodes)
        if name is not None:
            object.__setattr__(q, "name", name)
        queries.append(q)

    result = audit(register, queries, standalone=not args.no_standalone)
    text = render_json(result, indent=args.indent) if args.json else render_text(result)
    text = text if text.endswith("\n") else text + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0 if result.status == "ok" else 2


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            return _run_audit(args)
        return 1
    except AuditError as exc:
        sys.stderr.write(json.dumps(exc.as_dict(), ensure_ascii=False) + "\n")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
