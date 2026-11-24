#!/usr/bin/env python3
"""Convenience wrapper: run an audit from a query file, fully offline.

A query file is a JSON document:

    {
      "register": "register.json",
      "readings": "readings.json",
      "queries": [
        {"name": "june", "period": "2024-06", "nodes": [11, 22]},
        {"period": "2024-07", "nodes": [13]}
      ],
      "standalone": true
    }

Paths inside the file are resolved relative to the file itself, so the whole
run can be reproduced from a directory.  Use ``-`` for the query file to read
stdin.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wateraudit import (AuditError, Query, audit, load_register, render_json,
                        render_text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run a wateraudit query file")
    ap.add_argument("query", help="query JSON path ('-' for stdin)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--indent", type=int, default=2)
    ap.add_argument("--out", help="write output here instead of stdout")
    args = ap.parse_args(argv)

    try:
        if args.query == "-":
            spec = json.load(sys.stdin)
            base = Path.cwd()
        else:
            qpath = Path(args.query).resolve()
            with open(qpath, "r", encoding="utf-8") as fh:
                spec = json.load(fh)
            base = qpath.parent

        register_path = base / spec["register"]
        readings_path = base / spec["readings"]
        register = load_register(str(register_path), str(readings_path))

        queries = []
        for item in spec.get("queries", []):
            q = Query(item["period"], item["nodes"])
            if item.get("name") is not None:
                object.__setattr__(q, "name", item["name"])
            queries.append(q)
        if not queries:
            raise AuditError(AuditError.QUERY_INVALID, "query file lists no queries")

        result = audit(register, queries,
                       standalone=bool(spec.get("standalone", True)))
        text = render_json(result, indent=args.indent) if args.json else render_text(result)
        text = text if text.endswith("\n") else text + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0 if result.status == "ok" else 2
    except AuditError as exc:
        sys.stderr.write(json.dumps(exc.as_dict(), ensure_ascii=False) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
