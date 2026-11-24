# wateraudit — offline water-usage reconciliation

Some water points have no meter of their own, so their usage can only be
reconciled from a mix of total meters and sub-meters. This tool answers, for a
named billing period and a named set of meters:

* the **smallest and largest integer usage** the evidence allows for the union
  of real water points those meters cover, and
* one **complete per-zone account** for each endpoint, so every number can be
  re-checked line by line against the register and the raw readings.

A figure is safe to invoice at a fixed quantity **only when both endpoints are
equal** (`"invoiceable": true`). Missing evidence is never turned into zero,
and no customer gets a fixed-quantity bill without a unique usage.

The tool is pure Python 3.11 (standard library only), fully offline, and runs
in time linear in the register size, the number of readings and the number of
queries. Cost does not grow with usage magnitudes or reading-interval widths;
no usage allocation is ever enumerated.

---

## The model

The register is a **forest**. Each node is a zone; the zone total is its own
exclusive usage plus the totals of its direct children:

```
total(v) = own(v) + Σ total(c)          for c a direct child of v
own(v)   ∈ {0, 1, …, cap(v)}            cap optional; unbounded if absent
```

A **reading** is named evidence for one `(period, node)` pair, an inclusive
integer interval `[min, max]`. An exact reading has `min == max`.

* Every reading of a node in the same period **must hold**; several readings
  for the same node/period are intersected (the tightest interval).
* A node with **no reading** has no extra condition; its own usage is free in
  `[0, cap]`. An un-read node is never assumed to be zero.
* A **period is self-contained**. Nothing is interpolated or carried between
  periods. A reading for July is evidence for July only.
* A reading `[lo, hi]` on a tree **root** is the **total-meter bound** for that
  period. Every tree must have reading evidence of a *finite* upper bound on its
  root for the period being audited.

### Coverage and the union

A query names zones. The covered water points are the **union** of the selected
subtrees' own water points. Selecting both an ancestor and a descendant does not
double-count the descendant: it is already inside the ancestor's subtree. In
formula, with `w(v) = 1` iff `v` or an ancestor of `v` is selected, the query
usage is

```
Z = Σ_v w(v) · own(v)
```

Queries may span several trees. Trees are independent, so the interval of a
cross-tree query is the sum of the per-tree intervals, and the per-tree
certificates together form the complete account.

---

## Result statuses

| status | meaning |
|---|---|
| `feasible` | An interval `[usage_min, usage_max]` was computed, plus two certificates. |
| `infeasible` | A **usage conflict**: the evidence for a tree in this period admits no integer assignment. No interval is produced. The conflict quotes the raw readings and the local partition arithmetic needed to re-derive it. |
| `input_incomplete` | **Input problem, not a conflict**: a tree total has no reading evidence of a finite upper bound for the period. No interval is produced. |
| `input_invalid` | **Input problem**: broken parent link, cycle, duplicate id, unknown query node, malformed number. |

Structural input problems (`broken link`, `cycle`, `duplicate id`, non-integer
or out-of-range numbers) are raised as `AuditError` at load time. A missing
finite root bound is reported per `(period, tree)` as `input_incomplete`, so a
single bad period does not affect others.

A tree with a usage conflict makes **that period** unreportable for that tree;
it does not produce an interval and it is not confused with invalid input.

---

## Input formats

### Register — `register.json`

```json
{
  "nodes": [
    {"id": 10, "parent": null, "capacity": null},
    {"id": 11, "parent": 10, "capacity": 60},
    {"id": 12, "parent": 10, "capacity": 80}
  ]
}
```

* `id` — integer, unique.
* `parent` — integer parent id, or `null` for a root.
* `capacity` — non-negative integer upper bound on **own** usage, or `null`
  (unbounded) / omitted.

`capacity` bounds a zone's own usage only; the zone total is bounded by the
tree root's total-meter reading.

### Readings — `readings.json`

Either a bare list or `{"readings": [ … ]}`:

```json
{
  "readings": [
    {"period": "2024-06", "node": 10, "min": 1000, "max": 1120, "source": "total-A-06"},
    {"period": "2024-06", "node": 13, "min": 120,  "max": 120,  "source": "leaf-13-06"}
  ]
}
```

* `period` — `YYYY-N` (e.g. `2024-06`), or `LABEL@YYYY-N` to give a display
  label. Period identity is `(year, N)` only; periods are never interpolated.
* `node` — zone id from the register.
* `min` / `max` — inclusive integer bounds; `max` defaults to `min`.
  Aliases `lo`/`hi` are accepted. Floats and booleans are **rejected** rather
  than silently coerced.
* `source` — free-form provenance string, echoed in conflict reports.

A reading whose `node` is not in the register is **dangling evidence** and is
rejected (`INPUT_INCOMPLETE`, with the offending ids and sample records) rather
than quietly ignored — otherwise a typo could silently understate a bill.

---

## Running it

```
python3 -m wateraudit audit \
    --register examples/multi/register.json \
    --readings examples/multi/readings.json \
    --period 2024-06 --nodes 11,22 --name june-two-sites \
    --period 2024-07 --nodes 13      --name july-unread-leaf \
    --json
```

* `--period` is repeatable; each pairs with the `--nodes` at the same position
  (one `--nodes` is reused for every period).  `--name` pairs the same way.
* `--nodes` accepts comma lists and ranges: `3,4,10-12`.
* `--json` emits the machine format; otherwise a human-readable account.
* `--out FILE` writes the result to a file (the CLI's exit code follows the
  overall status).
* `--no-standalone` skips the per-tree period feasibility table.

Exit codes: `0` all queries reportable; `2` some query is infeasible or has an
input problem; `1` a load/validation error (JSON error object on stderr).

### Repeatable run from a query file

`run_audit.py` reads a whole audit (paths, periods, selections) from one JSON
file and resolves the data paths relative to that file, so the run can be
reproduced from the directory alone:

```json
{
  "register": "register.json",
  "readings": "readings.json",
  "queries": [
    {"name": "june-two-sites", "period": "2024-06", "nodes": [11, 22]},
    {"name": "july-unread-leaf", "period": "2024-07", "nodes": [13]}
  ]
}
```

```
python3 run_audit.py examples/multi/query.json --json
```

### Programmatic use

```python
from wateraudit import build_register, load_register, Query, audit, verify_certificate

reg = load_register("register.json", "readings.json")
result = audit(reg, [Query("2024-06", [11, 22])])
q = result.queries[0]
print(q.usage_min, q.usage_max, q.invoiceable)
for tree in q.trees:
    for endpoint in ("min", "max"):
        cert = tree["certificates"][endpoint]
        assert verify_certificate(reg, cert) == []   # independent re-check
```

---

## Output format

Top level (`--json`):

```jsonc
{
  "status": "ok" | "partial" | "not_reportable",
  "periods": ["2024-06", "2024-07"],
  "standalone": [                     // per (period, tree) feasibility table
    {"period": "2024-06", "tree": 0, "root": 10, "status": "feasible",
     "usage_min": 0, "usage_max": 0}
  ],
  "queries": [
    {
      "name": "june-two-sites",
      "period": "2024-06",
      "selected": [11, 22],
      "status": "feasible",
      "usage_min": 520,
      "usage_max": 1180,
      "invoiceable": false,
      "trees": [
        {
          "tree_root": 10, "status": "feasible", "selected": [11],
          "usage_min": 120, "usage_max": 280,
          "certificates": {
            "min": { /* account for the minimum */ },
            "max": { /* account for the maximum */ }
          }
        }
      ],
      "conflicts": [],
      "errors": []
    }
  ]
}
```

### A certificate

```jsonc
{
  "endpoint": "min",
  "root": 10,
  "period": "2024-06",
  "usage": 120,
  "nodes": [
    {
      "node": 10, "parent": -1, "weight": 0,
      "own": 380, "total": 1000, "capacity": null,
      "reading": [1000, 1120],
      "readings": [{"period": "2024-06", "node": 10, "min": 1000, "max": 1120,
                    "source": "total-A-06"}]
    }
    // ... every node of the tree, parents before children
  ]
}
```

* `own` is the zone's exclusive usage; `total` is the zone total.
* `weight` is 1 when the node is covered by the query (it or an ancestor was
  selected). `usage` equals `Σ weight · own`.
* `reading` is the interval that applied to `total`; `readings` repeats the raw
  records for provenance.

The two certificates may differ — they are two different feasible allocations
that attain the two endpoints.

### Conflict reports

An `infeasible` result carries, for each affected tree, the local cause:

```jsonc
{"kind": "subtree_infeasible", "node": 31,
 "node_readings": [{"period": "2024-05", "node": 31, "min": 150, "max": 150,
                    "source": "sub-31-05"}],
 "own_capacity": 40,
 "children_required_min": {"33": 0}, "children_required_max": {"33": 30},
 "required_interval": [150, 70],
 "explanation": "total(31) must be >= 150 … and <= 70 …",
 "children_evidence": [{"node": 33, "capacity": 30, "readings": [],
                        "reachable_total": [0, 30]}]}
```

`readings_disagree` is emitted instead when two readings of the *same* node in
the *same* period do not intersect. In both cases the raw numbers needed to
re-check the contradiction are included.

---

## Independent verification

`wateraudit.verify_certificate(register, certificate)` re-reads the register and
raw readings (it does not trust solver intermediates) and checks, for every line:

* `0 ≤ own ≤ cap`;
* `total = own + Σ child totals`;
* `total` lies inside every reading of that node in the period;
* the certificate's `usage` equals `Σ weight · own`;
* the account is complete for its tree and contains no foreign node.

Any discrepancy is returned as a structured problem with the offending node and
the raw evidence.

---

## Why it is fast

The reachable totals of a subtree form an **integer interval** `[m(v), M(v)]`,
computed bottom-up:

```
m(v) = max(lo(v), Σ m(c))
M(v) = min(hi(v), cap(v) + Σ M(c))
```

Because the query weight is constant on each selected subtree, the min and max
of the covered usage are obtained by two local dynamic programs whose state at
each node is a single interval. Both are O(1) per node and the whole solve is
O(V + E + R) for one tree and one period, with integer arithmetic only. Building
a certificate is one more top-down pass. Deep trees (100k-node chains) and wide
star trees (100k children) are handled iteratively — no recursion, no allocation
enumeration, no dependence on how large the numbers or intervals are.

The two DPs and the certificate construction are validated exhaustively against
a brute-force oracle over thousands of random small forests
(`tests/test_bruteforce.py`).

---

## Layout

```
wateraudit/
  model.py     register/readings/query parsing and validation, forest indexing
  solver.py    per-tree DP, endpoint values, certificate construction
  audit.py     period checks, cross-tree union queries, conflict reporting
  verify.py    independent certificate re-check
  report.py    JSON and text rendering
  cli.py       command-line entry point (python -m wateraudit)
tests/         brute-force oracle, behaviour, structure/perf, verifier + CLI
examples/      simple, multi (cross-tree, multi-period), conflict
run_audit.py   run an audit from a self-contained query JSON file
```

Run the tests with:

```
python3 -m unittest discover -s tests -p 'test_*.py'
```

`tests/test_bruteforce.py` compares the tool against an exhaustive oracle over
thousands of random forests (including interval-shape and uncovered-ancestor
properties); `tests/test_structure.py` exercises 100k-node chains, 100k-child
stars and trillion-scale numbers against a time budget.
