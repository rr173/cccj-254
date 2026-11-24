"""Exhaustive randomized comparison against a brute-force oracle.

For thousands of small random forests we enumerate every own-usage vector and
compare the package's interval (and both certificates) with the truth.
"""

from __future__ import annotations

import random
import unittest

from helpers import PERIOD, brute_force, make_forest, preorder, random_tree

from wateraudit import Query, audit, build_register, verify_certificate


def random_case(rnd: random.Random):
    n = rnd.randint(1, 5)
    children, parent = random_tree(rnd, n, rnd.choice(["chain", "star", "mixed"]))
    order = preorder(children)
    cap = {}
    lo = {}
    hi = {}
    readings = {}
    for v in order:
        cap[v] = rnd.choice([0, 1, 2, 3, 4, rnd.randint(5, 8)])
        if rnd.random() < 0.5:
            a = rnd.randint(0, 5)
            b = a + rnd.randint(0, 3)
            lo[v], hi[v] = a, b
            readings[v] = (a, b)
        else:
            lo[v], hi[v] = 0, float("inf")
    # The root total needs a finite upper bound (else: invalid input).
    root_top = rnd.randint(max(lo[0], 1), max(lo[0], 1) + 6)
    if hi[0] > root_top:
        hi[0] = root_top
        readings[0] = (lo[0], root_top)
    selected = {v for v in order if rnd.random() < 0.4}
    if not selected:
        selected = {order[-1]}
    spec = {"trees": [{"parent": parent, "cap": cap, "readings": readings}]}
    return spec, children, parent, cap, lo, hi, selected


class BruteForceTest(unittest.TestCase):
    def test_against_brute_force(self):
        checked = 0
        for seed in range(4000):
            rnd = random.Random(seed)
            spec, children, parent, cap, lo, hi, selected = random_case(rnd)
            reg = make_forest(spec)
            expected_min, expected_max = brute_force(
                children, parent, cap, lo, hi, 0, selected)
            if expected_min == "skip":
                continue
            result = audit(reg, [Query(PERIOD, sorted(selected))])
            q = result.queries[0]
            with self.subTest(seed=seed):
                if expected_min is None:
                    self.assertEqual(q.status, "infeasible")
                    self.assertIsNone(q.usage_min)
                    self.assertIsNone(q.usage_max)
                    continue
                self.assertEqual(q.status, "feasible")
                self.assertEqual((q.usage_min, q.usage_max),
                                 (expected_min, expected_max))
                self.assertEqual(q.invoiceable, expected_min == expected_max)
                for tree in q.trees:
                    for ep in ("min", "max"):
                        problems = verify_certificate(reg, tree["certificates"][ep])
                        self.assertEqual(problems, [], f"seed {seed} {ep}: {problems}")
            checked += 1
        self.assertGreater(checked, 2000)


class IntervalProperty(unittest.TestCase):
    """The DP's key structural assumption: reachable totals are interval-shaped."""

    def test_reachable_totals_are_contiguous(self):
        import itertools
        from helpers import preorder

        for seed in range(2000):
            rnd = random.Random(seed + 100_000)
            spec, children, parent, cap, lo, hi, _sel = random_case(rnd)
            order = preorder(children, 0)
            capv = {v: (cap[v] if cap[v] is not None else 6) for v in order}
            hoisted = {v: (hi[v] if hi[v] != float("inf") else 10 ** 6) for v in order}
            space = 1
            for v in order:
                space *= capv[v] + 1
            if space > 40_000:
                continue
            reach = set()
            for combo in itertools.product(*[range(capv[v] + 1) for v in order]):
                own = dict(zip(order, combo))
                tot = {}
                ok = True
                for v in reversed(order):
                    s = own[v] + sum(tot[c] for c in children[v])
                    if s < lo[v] or s > hoisted[v]:
                        ok = False
                        break
                    tot[v] = s
                if ok:
                    reach.add(tot[0])
            if reach:
                self.assertEqual(reach, set(range(min(reach), max(reach) + 1)),
                                 f"seed {seed}: reachable root totals not contiguous")


class UncoveredAncestors(unittest.TestCase):
    """Selecting only deep nodes leaves big-capacity ancestors uncovered.

    Their own usage is not part of the query, so they should absorb slack
    freely; this is the case most likely to break the certificate pass.
    """

    def test_only_descendants_selected(self):
        import itertools
        from helpers import random_tree

        for seed in range(1500):
            rnd = random.Random(seed + 700_000)
            n = rnd.randint(2, 5)
            children, parent = random_tree(rnd, n, rnd.choice(["chain", "star", "mixed"]))
            order = preorder(children)
            cap = {v: rnd.choice([0, 2, 5, 12, 25]) for v in order}
            lo, hi = {}, {}
            space = 1
            for v in order:
                space *= cap[v] + 1
                if rnd.random() < 0.6:
                    a = rnd.randint(0, 6)
                    lo[v], hi[v] = a, a + rnd.randint(0, 2)
                else:
                    lo[v], hi[v] = 0, 10 ** 6
            if hi[0] > 60:
                hi[0] = rnd.randint(lo[0], 60)
            if space > 120_000:
                continue

            nodes = [{"id": v, "parent": parent[v], "capacity": cap[v]} for v in order]
            readings = [{"period": PERIOD, "node": v, "min": lo[v], "max": hi[v]}
                        for v in order if hi[v] < 10 ** 6 or lo[v] > 0]
            if not any(r["node"] == 0 for r in readings):
                readings.append({"period": PERIOD, "node": 0, "min": 0, "max": hi[0]})
            reg = build_register({"nodes": nodes}, readings)

            selected = {v for v in order if parent[v] is not None and rnd.random() < 0.35}
            if not selected:
                selected = {order[-1]}
            w = {}
            for v in order:
                w[v] = 1 if (v in selected or (parent[v] is not None and w[parent[v]])) else 0

            mn = mx = None
            for combo in itertools.product(*[range(cap[v] + 1) for v in order]):
                own = dict(zip(order, combo))
                tot = {}
                ok = True
                for v in reversed(order):
                    s = own[v] + sum(tot[c] for c in children[v])
                    if s < lo[v] or s > hi[v]:
                        ok = False
                        break
                    tot[v] = s
                if ok:
                    z = sum(w[v] * own[v] for v in order)
                    mn = z if mn is None else min(mn, z)
                    mx = z if mx is None else max(mx, z)
            if mn is None:
                continue
            q = audit(reg, [Query(PERIOD, sorted(selected))], standalone=False).queries[0]
            with self.subTest(seed=seed):
                self.assertEqual(q.status, "feasible")
                self.assertEqual((q.usage_min, q.usage_max), (mn, mx))
                for tree in q.trees:
                    for ep in ("min", "max"):
                        self.assertEqual(verify_certificate(reg, tree["certificates"][ep]), [])


if __name__ == "__main__":
    unittest.main()
