"""Structural and performance tests.

The contract promises cost independent of usage magnitude and interval width.
These tests build large registers (100k nodes), deep chains (no recursion), and
huge numbers, and assert both correctness and a time budget.
"""

from __future__ import annotations

import time
import unittest

from helpers import PERIOD

from wateraudit import Query, audit, build_register, verify_certificate


def big_registry(n, cap=5, readings=None):
    nodes = [{"id": i, "parent": None if i == 0 else (i - 1) // 2, "capacity": cap}
             for i in range(n)]
    return build_register({"nodes": nodes}, readings or [])


class DeepChain(unittest.TestCase):
    def test_100k_deep_chain_no_recursion(self):
        n = 100_000
        nodes = [{"id": i, "parent": None if i == 0 else i - 1, "capacity": 1}
                 for i in range(n)]
        readings = [
            {"period": PERIOD, "node": 0, "min": 1000, "max": 2000},
        ]
        r = build_register({"nodes": nodes}, readings)
        t0 = time.time()
        q = audit(r, [Query(PERIOD, [n - 1])], standalone=False).queries[0]
        elapsed = time.time() - t0
        self.assertEqual(q.status, "feasible")
        self.assertEqual((q.usage_min, q.usage_max), (0, 1))
        self.assertLess(elapsed, 10.0, f"deep chain took {elapsed:.2f}s")

    def test_deep_chain_certificate_verifies(self):
        n = 5000
        nodes = [{"id": i, "parent": None if i == 0 else i - 1, "capacity": 2}
                 for i in range(n)]
        readings = [{"period": PERIOD, "node": 0, "min": 1000, "max": 1000}]
        r = build_register({"nodes": nodes}, readings)
        q = audit(r, [Query(PERIOD, [n - 1])], standalone=False).queries[0]
        self.assertEqual(q.status, "feasible")
        cert = q.trees[0]["certificates"]["max"]
        self.assertEqual(verify_certificate(r, cert), [])
        self.assertEqual(len(cert["nodes"]), n)


class WideTree(unittest.TestCase):
    def test_100k_binary_tree(self):
        n = 100_000
        readings = [
            {"period": PERIOD, "node": 0, "min": 0, "max": 300_000},
            {"period": PERIOD, "node": 1, "min": 1000, "max": 2000},
        ]
        t0 = time.time()
        r = big_registry(n, cap=5, readings=readings)
        q = audit(r, [Query(PERIOD, list(range(1, 100)))],
                  standalone=False).queries[0]
        elapsed = time.time() - t0
        self.assertEqual(q.status, "feasible")
        self.assertLess(elapsed, 10.0, f"wide tree took {elapsed:.2f}s")
        for tree in q.trees:
            for ep in ("min", "max"):
                self.assertEqual(verify_certificate(r, tree["certificates"][ep]), [])

    def test_standalone_scan_of_100k(self):
        n = 100_000
        readings = [{"period": PERIOD, "node": 0, "min": 0, "max": 10 ** 12}]
        r = big_registry(n, cap=3, readings=readings)
        t0 = time.time()
        res = audit(r, [Query(PERIOD, [n - 1])])
        elapsed = time.time() - t0
        self.assertEqual(res.standalone[0]["status"], "feasible")
        self.assertLess(elapsed, 15.0, f"standalone scan took {elapsed:.2f}s")


class HugeNumbers(unittest.TestCase):
    def test_10_trillion_intervals(self):
        big = 10 ** 13
        nodes = [
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": big},
            {"id": 2, "parent": 1, "capacity": big},
        ]
        readings = [
            {"period": PERIOD, "node": 0, "min": 0, "max": 2 * big},
            {"period": PERIOD, "node": 2, "min": big - 1, "max": big},
        ]
        r = build_register({"nodes": nodes}, readings)
        t0 = time.time()
        q = audit(r, [Query(PERIOD, [2])], standalone=False).queries[0]
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual((q.usage_min, q.usage_max), (big - 1, big))
        self.assertEqual(verify_certificate(r, q.trees[0]["certificates"]["max"]), [])


class StarWide(unittest.TestCase):
    def test_100k_children(self):
        n = 100_000
        nodes = [{"id": 0, "parent": None, "capacity": None}]
        nodes += [{"id": i, "parent": 0, "capacity": 7} for i in range(1, n)]
        readings = [{"period": PERIOD, "node": 0, "min": 0, "max": 10 ** 12},
                    {"period": PERIOD, "node": 5, "min": 3, "max": 3}]
        r = build_register({"nodes": nodes}, readings)
        t0 = time.time()
        q = audit(r, [Query(PERIOD, list(range(1, n)))],
                  standalone=False).queries[0]
        elapsed = time.time() - t0
        self.assertEqual(q.status, "feasible")
        self.assertEqual(q.usage_min, 3)
        self.assertLess(elapsed, 10.0, f"star took {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
