"""Behavioural tests for the audit contract.

Each test names the rule it protects, so failures point at a documented
requirement rather than at an implementation detail.
"""

from __future__ import annotations

import json
import unittest

from helpers import PERIOD

from wateraudit import (AuditError, BillingPeriod, Query, audit, build_register,
                        render_json, verify_certificate)


def reg(nodes, readings=(), periods=None):
    return build_register({"nodes": nodes}, list(readings), periods)


def rd(node, a, b=None, period=PERIOD):
    return {"period": period, "node": node, "min": a,
            "max": a if b is None else b}


class MissingReadingIsNotZero(unittest.TestCase):
    def test_unread_zone_keeps_interval_open(self):
        # Un-read child: its own usage may be anything up to capacity, so the
        # parent total is not forced to equal the read child.
        r = reg([
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 10},
            {"id": 2, "parent": 0, "capacity": 7},
        ], [rd(0, 10, 20), rd(1, 3, 3)])
        q = audit(r, [Query(PERIOD, [2])]).queries[0]
        self.assertEqual(q.status, "feasible")
        # node 2 has no reading -> [0, 7]; the total must be able to absorb it.
        self.assertEqual((q.usage_min, q.usage_max), (0, 7))

    def test_unread_subtree_not_billed_zero(self):
        r = reg([
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 5},
        ], [rd(0, 100, 100)])
        q = audit(r, [Query(PERIOD, [1])]).queries[0]
        self.assertEqual((q.usage_min, q.usage_max), (0, 5))


class ReadingsMustAllHold(unittest.TestCase):
    def test_multiple_readings_intersect(self):
        r = reg([
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 50},
        ], [rd(0, 100, 200), rd(1, 10, 40), rd(1, 20, 30)])
        q = audit(r, [Query(PERIOD, [1])]).queries[0]
        self.assertEqual((q.usage_min, q.usage_max), (20, 30))

    def test_disjoint_readings_are_a_usage_conflict(self):
        r = reg([
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 50},
        ], [rd(0, 100, 200), rd(1, 10, 20), rd(1, 30, 40)])
        q = audit(r, [Query(PERIOD, [1])]).queries[0]
        self.assertEqual(q.status, "infeasible")
        kinds = [c["kind"] for c in q.conflicts[0]["conflicts"]]
        self.assertIn("readings_disagree", kinds)
        quoted = q.conflicts[0]["conflicts"][0]["readings"]
        self.assertEqual(len(quoted), 2)
        self.assertEqual({(x["min"], x["max"]) for x in quoted},
                         {(10, 20), (30, 40)})

    def test_exact_reading_is_hard_evidence(self):
        r = reg([
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 50},
        ], [rd(0, 100, 200), rd(1, 25, 25)])
        q = audit(r, [Query(PERIOD, [1])]).queries[0]
        self.assertEqual((q.usage_min, q.usage_max), (25, 25))
        self.assertTrue(q.invoiceable)


class PeriodsAreIndependent(unittest.TestCase):
    def test_no_interpolation_across_periods(self):
        nodes = [
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 50},
        ]
        r = reg(nodes, [rd(0, 0, 100, "2024-01"), rd(1, 10, 10, "2024-01"),
                        rd(0, 0, 100, "2024-03"), rd(1, 30, 30, "2024-03"),
                        rd(0, 0, 100, "2024-02")])
        q1 = audit(r, [Query("2024-01", [1])]).queries[0]
        q2 = audit(r, [Query("2024-03", [1])]).queries[0]
        q3 = audit(r, [Query("2024-02", [1])]).queries[0]
        self.assertEqual((q1.usage_min, q1.usage_max), (10, 10))
        self.assertEqual((q2.usage_min, q2.usage_max), (30, 30))
        # February has no node-1 reading: evidence is the root bound alone, so
        # the usage is the open [0, 50] - never an interpolated 20.
        self.assertEqual((q3.usage_min, q3.usage_max), (0, 50))

    def test_period_specific_reading_does_not_leak(self):
        nodes = [
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 50},
        ]
        # Reading only for January; February is evidenced by the root alone.
        r = reg(nodes, [rd(1, 10, 10, "2024-01"), rd(0, 0, 100, "2024-01"),
                        rd(0, 0, 100, "2024-02")])
        q = audit(r, [Query("2024-02", [1])]).queries[0]
        self.assertEqual((q.usage_min, q.usage_max), (0, 50))


class SelectionUnion(unittest.TestCase):
    def setUp(self):
        #  0
        #  ├─1─┬─3
        #  │   └─4
        #  └─2
        self.r = reg([
            {"id": 0, "parent": None, "capacity": 1},
            {"id": 1, "parent": 0, "capacity": 2},
            {"id": 2, "parent": 0, "capacity": 2},
            {"id": 3, "parent": 1, "capacity": 2},
            {"id": 4, "parent": 1, "capacity": 2},
        ], [rd(0, 0, 100)])

    def test_ancestor_and_descendant_counted_once(self):
        q_all = audit(self.r, [Query(PERIOD, [1, 2])]).queries[0]
        q_dup = audit(self.r, [Query(PERIOD, [1, 3, 4, 2])]).queries[0]
        self.assertEqual((q_all.usage_min, q_all.usage_max),
                         (q_dup.usage_min, q_dup.usage_max))

    def test_duplicate_selection_is_idempotent(self):
        a = audit(self.r, [Query(PERIOD, [3])]).queries[0]
        b = audit(self.r, [Query(PERIOD, [3, 3, 3])]).queries[0]
        self.assertEqual((a.usage_min, a.usage_max), (b.usage_min, b.usage_max))

    def test_ancestor_covers_subtree(self):
        q1 = audit(self.r, [Query(PERIOD, [1])]).queries[0]
        q_children = audit(self.r, [Query(PERIOD, [3, 4])]).queries[0]
        # Selecting 1 must cover at least as much as selecting its children.
        self.assertGreaterEqual(q1.usage_max, q_children.usage_max)


class CrossTree(unittest.TestCase):
    def test_interval_is_sum_across_trees(self):
        nodes = [
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 10},
            {"id": 2, "parent": None, "capacity": None},
            {"id": 3, "parent": 2, "capacity": 20},
        ]
        r = reg(nodes, [rd(0, 5, 6), rd(1, 0, 4), rd(2, 7, 9), rd(3, 2, 3)])
        single_a = audit(r, [Query(PERIOD, [1])]).queries[0]
        single_b = audit(r, [Query(PERIOD, [3])]).queries[0]
        both = audit(r, [Query(PERIOD, [1, 3])]).queries[0]
        self.assertEqual(both.usage_min, single_a.usage_min + single_b.usage_min)
        self.assertEqual(both.usage_max, single_a.usage_max + single_b.usage_max)
        self.assertEqual(len(both.trees), 2)
        for tree in both.trees:
            for ep in ("min", "max"):
                self.assertEqual(verify_certificate(r, tree["certificates"][ep]), [])


class InfeasibleTreePerPeriod(unittest.TestCase):
    def test_conflict_reports_raw_readings_and_partition_constraints(self):
        nodes = [
            {"id": 0, "parent": None, "capacity": 0},
            {"id": 1, "parent": 0, "capacity": 1},
        ]
        r = reg(nodes, [rd(0, 3, 4), rd(1, 0, 3)])
        q = audit(r, [Query(PERIOD, [1])]).queries[0]
        self.assertEqual(q.status, "infeasible")
        self.assertIsNone(q.usage_min)
        self.assertIsNone(q.usage_max)
        conf = q.conflicts[0]["conflicts"][0]
        self.assertEqual(conf["kind"], "subtree_infeasible")
        self.assertEqual(conf["node"], 0)
        self.assertEqual(conf["required_interval"][0], 3)
        self.assertEqual(conf["required_interval"][1], 1)
        self.assertEqual(conf["node_readings"], [rd(0, 3, 4)])
        self.assertIn("children_required_min", conf)

    def test_conflict_does_not_poison_other_period(self):
        nodes = [
            {"id": 0, "parent": None, "capacity": 0},
            {"id": 1, "parent": 0, "capacity": 1},
        ]
        r = reg(nodes, [rd(0, 3, 4, "2024-01"), rd(1, 0, 3, "2024-01"),
                        rd(0, 0, 1, "2024-02"), rd(1, 0, 1, "2024-02")])
        res = audit(r, [Query("2024-01", [1]), Query("2024-02", [1])])
        self.assertEqual(res.queries[0].status, "infeasible")
        self.assertEqual(res.queries[1].status, "feasible")
        self.assertEqual(res.status, "partial")

    def test_conflict_does_not_poison_other_tree(self):
        nodes = [
            {"id": 0, "parent": None, "capacity": 0},
            {"id": 1, "parent": 0, "capacity": 1},
            {"id": 2, "parent": None, "capacity": None},
            {"id": 3, "parent": 2, "capacity": 4},
        ]
        r = reg(nodes, [rd(0, 3, 4), rd(1, 0, 3), rd(2, 5, 5), rd(3, 1, 2)])
        q = audit(r, [Query(PERIOD, [3])]).queries[0]
        self.assertEqual(q.status, "feasible")
        res = audit(r, [Query(PERIOD, [1, 3])])
        # A query spanning a conflicted tree cannot be reported, even though the
        # other tree is fine - and it must not invent a partial number.  The
        # per-tree rows document which tree failed and which one was solvable.
        self.assertEqual(res.queries[0].status, "infeasible")
        self.assertIsNone(res.queries[0].usage_min)
        statuses = {t["tree_root"]: t["status"] for t in res.queries[0].trees}
        self.assertEqual(statuses[2], "feasible")
        self.assertEqual(statuses[0], "infeasible")


class InputProblems(unittest.TestCase):
    def test_missing_finite_root_bound_is_not_a_conflict(self):
        nodes = [
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 5},
        ]
        r = reg(nodes, [rd(1, 1, 2)])   # root has no reading at all
        q = audit(r, [Query(PERIOD, [1])]).queries[0]
        self.assertEqual(q.status, "input_incomplete")
        self.assertIsNone(q.usage_min)
        self.assertTrue(q.errors)
        self.assertFalse(q.conflicts)

    def test_infinite_root_reading_is_not_a_conflict(self):
        nodes = [
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 5},
        ]
        # Reading at the root says nothing about an upper bound?  Our format
        # always has a finite max, so use the absence of a reading instead.
        r = reg(nodes, [rd(1, 1, 2)], periods=[PERIOD])
        q = audit(r, [Query(PERIOD, [1])]).queries[0]
        self.assertEqual(q.status, "input_incomplete")

    def test_broken_parent_link(self):
        with self.assertRaises(AuditError) as ctx:
            reg([{"id": 0, "parent": 99, "capacity": 1}])
        self.assertEqual(ctx.exception.code, AuditError.INPUT_INCOMPLETE)

    def test_self_parent_is_broken_not_a_cycle(self):
        with self.assertRaises(AuditError) as ctx:
            reg([{"id": 0, "parent": 0, "capacity": 1}])
        self.assertEqual(ctx.exception.code, AuditError.INPUT_INCOMPLETE)

    def test_multi_node_cycle_is_invalid(self):
        with self.assertRaises(AuditError) as ctx:
            reg([{"id": 0, "parent": 1, "capacity": 1},
                 {"id": 1, "parent": 0, "capacity": 1}])
        self.assertEqual(ctx.exception.code, AuditError.INPUT_INVALID)

    def test_duplicate_node_id(self):
        with self.assertRaises(AuditError) as ctx:
            reg([{"id": 0, "parent": None}, {"id": 0, "parent": None}])
        self.assertEqual(ctx.exception.code, AuditError.INPUT_INVALID)

    def test_unknown_query_node(self):
        r = reg([{"id": 0, "parent": None, "capacity": 1}])
        with self.assertRaises(AuditError) as ctx:
            audit(r, [Query(PERIOD, [42])])
        self.assertEqual(ctx.exception.code, AuditError.QUERY_INVALID)

    def test_negative_capacity(self):
        with self.assertRaises(AuditError) as ctx:
            reg([{"id": 0, "parent": None, "capacity": -1}])
        self.assertEqual(ctx.exception.code, AuditError.INPUT_INVALID)

    def test_float_reading_rejected(self):
        with self.assertRaises(AuditError) as ctx:
            reg([{"id": 0, "parent": None}],
                [{"period": PERIOD, "node": 0, "min": 1.0, "max": 2}])
        self.assertEqual(ctx.exception.code, AuditError.INPUT_INVALID)

    def test_reading_min_above_max(self):
        with self.assertRaises(AuditError) as ctx:
            reg([{"id": 0, "parent": None}], [rd(0, 5, 4)])
        self.assertEqual(ctx.exception.code, AuditError.INPUT_INVALID)


class Invoiceability(unittest.TestCase):
    def test_equal_endpoints_are_invoiceable(self):
        r = reg([
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 10},
        ], [rd(0, 30, 40), rd(1, 7, 7)])
        q = audit(r, [Query(PERIOD, [1])]).queries[0]
        self.assertTrue(q.invoiceable)
        self.assertEqual(q.usage_min, q.usage_max)

    def test_wide_endpoints_not_invoiceable(self):
        r = reg([
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 10},
        ], [rd(0, 30, 40)])
        q = audit(r, [Query(PERIOD, [1])]).queries[0]
        self.assertFalse(q.invoiceable)

    def test_cross_tree_equal_endpoints(self):
        nodes = [
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 10},
            {"id": 2, "parent": None, "capacity": None},
            {"id": 3, "parent": 2, "capacity": 10},
        ]
        r = reg(nodes, [rd(0, 5, 5), rd(1, 2, 2), rd(2, 9, 9), rd(3, 3, 3)])
        q = audit(r, [Query(PERIOD, [1, 3])]).queries[0]
        self.assertTrue(q.invoiceable)
        self.assertEqual(q.usage_min, 5)


class LargeNumbers(unittest.TestCase):
    def test_trillion_scale_preserved_exactly(self):
        big = 10 ** 13
        r = reg([
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 10 ** 12},
        ], [rd(0, big - 10 ** 12, big), rd(1, 10 ** 12 - 1, 10 ** 12 - 1)])
        q = audit(r, [Query(PERIOD, [1])]).queries[0]
        self.assertEqual(q.usage_min, 10 ** 12 - 1)
        self.assertEqual(q.usage_max, 10 ** 12 - 1)
        payload = json.loads(render_json(audit(r, [Query(PERIOD, [1])])))
        self.assertEqual(payload["queries"][0]["usage_min"], 10 ** 12 - 1)

    def test_wide_interval_does_not_enum(self):
        # An interval a trillion wide must be handled by arithmetic, not loops.
        r = reg([
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": None},
        ], [rd(0, 0, 10 ** 12), rd(1, 0, 10 ** 12)])
        q = audit(r, [Query(PERIOD, [1])]).queries[0]
        self.assertEqual(q.usage_max, 10 ** 12)


class Canonicity(unittest.TestCase):
    def test_output_deterministic(self):
        r = reg([
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 10},
            {"id": 2, "parent": 0, "capacity": 10},
        ], [rd(0, 5, 9)])
        a = render_json(audit(r, [Query(PERIOD, [1, 2])]))
        b = render_json(audit(r, [Query(PERIOD, [1, 2])]))
        self.assertEqual(a, b)

    def test_certificate_min_and_max_may_differ(self):
        r = reg([
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 10},
        ], [rd(0, 5, 9)])
        q = audit(r, [Query(PERIOD, [1])]).queries[0]
        certs = q.trees[0]["certificates"]
        self.assertNotEqual(certs["min"]["usage"], certs["max"]["usage"])


class PeriodIdentity(unittest.TestCase):
    def test_label_does_not_change_identity(self):
        a = BillingPeriod.parse("JULY@2024-07")
        b = BillingPeriod.parse("2024-07")
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        self.assertEqual(a.key(), b.key())
        self.assertEqual(a.label, "JULY")

    def test_query_label_matches_reading_label(self):
        r = reg([{"id": 0, "parent": None, "capacity": None},
                 {"id": 1, "parent": 0, "capacity": 5}],
                [{"period": "JULY@2024-07", "node": 0, "min": 10, "max": 20,
                  "source": "t"}])
        q = audit(r, [Query("2024-07", [1])], standalone=False).queries[0]
        self.assertEqual(q.status, "feasible")
        self.assertEqual(q.as_dict()["period_key"], [2024, 7])

    def test_period_label_reported_from_register(self):
        r = reg([{"id": 0, "parent": None, "capacity": None}],
                [{"period": "JULY@2024-07", "node": 0, "min": 1, "max": 2}])
        q = audit(r, [Query("2024-07", [0])], standalone=False).queries[0]
        self.assertEqual(q.as_dict()["period"], "JULY")


class DanglingEvidence(unittest.TestCase):
    def test_reading_for_unknown_zone_is_input_incomplete(self):
        with self.assertRaises(AuditError) as ctx:
            reg([{"id": 0, "parent": None, "capacity": None},
                 {"id": 1, "parent": 0, "capacity": 5}],
                [rd(0, 10, 10), rd(99, 1, 2)])
        self.assertEqual(ctx.exception.code, AuditError.INPUT_INCOMPLETE)
        self.assertEqual(ctx.exception.detail["missing_nodes"], [99])
        self.assertTrue(ctx.exception.detail["sample_readings"])


if __name__ == "__main__":
    unittest.main()
