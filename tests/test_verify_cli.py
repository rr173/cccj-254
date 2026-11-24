"""Tests for the independent certificate verifier and the CLI entry point."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import PERIOD

from wateraudit import (Query, audit, build_register, render_json,
                        verify_certificate)


def reg(nodes, readings=()):
    return build_register({"nodes": nodes}, list(readings))


def rd(node, a, b=None, period=PERIOD):
    return {"period": period, "node": node, "min": a,
            "max": a if b is None else b}


class Verifier(unittest.TestCase):
    def setUp(self):
        self.r = reg([
            {"id": 0, "parent": None, "capacity": 3},
            {"id": 1, "parent": 0, "capacity": 5},
            {"id": 2, "parent": 0, "capacity": 5},
        ], [rd(0, 10, 14), rd(2, 2, 2)])
        self.q = audit(self.r, [Query(PERIOD, [1, 2])]).queries[0]
        self.cert = self.q.trees[0]["certificates"]["max"]

    def test_clean_certificate_passes(self):
        self.assertEqual(verify_certificate(self.r, self.cert), [])

    def test_tampered_total_detected(self):
        bad = json.loads(json.dumps(self.cert))
        bad["nodes"][1]["total"] += 1
        kinds = {p["kind"] for p in verify_certificate(self.r, bad)}
        self.assertIn("partition_violation", kinds)

    def test_tampered_own_detected(self):
        bad = json.loads(json.dumps(self.cert))
        for e in bad["nodes"]:
            if e["node"] == 1:
                e["own"] += 1
                e["total"] += 1
        problems = verify_certificate(self.r, bad)
        self.assertTrue(problems)
        kinds = {p["kind"] for p in problems}
        self.assertTrue(kinds & {"capacity" , "partition_violation", "usage_mismatch"})

    def test_capacity_violation_detected(self):
        bad = json.loads(json.dumps(self.cert))
        for e in bad["nodes"]:
            if e["node"] == 1:
                e["own"] = 999
                e["total"] = 999
        kinds = {p["kind"] for p in verify_certificate(self.r, bad)}
        self.assertIn("own_out_of_capacity", kinds)

    def test_reading_violation_detected(self):
        bad = json.loads(json.dumps(self.cert))
        for e in bad["nodes"]:
            if e["node"] == 2:
                e["own"] = 0
                # keep partition consistent by moving the total off the reading
                e["total"] = 0
        kinds = {p["kind"] for p in verify_certificate(self.r, bad)}
        self.assertIn("reading_violation", kinds)

    def test_incomplete_account_detected(self):
        bad = json.loads(json.dumps(self.cert))
        bad["nodes"] = [e for e in bad["nodes"] if e["node"] != 2]
        kinds = {p["kind"] for p in verify_certificate(self.r, bad)}
        self.assertIn("incomplete_account", kinds)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        register = {"nodes": [
            {"id": 0, "parent": None, "capacity": None},
            {"id": 1, "parent": 0, "capacity": 10},
            {"id": 2, "parent": 0, "capacity": 100},
        ]}
        readings = {"readings": [
            {"period": "2024-01", "node": 0, "min": 40, "max": 60},
            {"period": "2024-01", "node": 2, "min": 40, "max": 40},
            {"period": "2024-02", "node": 0, "min": 60, "max": 80},
        ]}
        self.reg_path = self.dir / "register.json"
        self.rd_path = self.dir / "readings.json"
        self.reg_path.write_text(json.dumps(register))
        self.rd_path.write_text(json.dumps(readings))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = "0"
        workdir = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = workdir
        return subprocess.run(
            [sys.executable, "-m", "wateraudit", "audit", *args],
            capture_output=True, text=True, cwd=workdir, env=env)

    def test_json_output_and_reproducibility(self):
        a = self.run_cli("--register", str(self.reg_path),
                         "--readings", str(self.rd_path),
                         "--period", "2024-01", "--nodes", "2", "--json")
        b = self.run_cli("--register", str(self.reg_path),
                         "--readings", str(self.rd_path),
                         "--period", "2024-01", "--nodes", "2", "--json")
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertEqual(a.stdout, b.stdout)
        payload = json.loads(a.stdout)
        q = payload["queries"][0]
        self.assertEqual(q["status"], "feasible")
        self.assertEqual((q["usage_min"], q["usage_max"]), (40, 40))
        self.assertTrue(q["invoiceable"])

    def test_multi_period(self):
        out = self.run_cli("--register", str(self.reg_path),
                           "--readings", str(self.rd_path),
                           "--period", "2024-01", "--nodes", "1",
                           "--period", "2024-02", "--nodes", "1", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual(len(payload["queries"]), 2)
        self.assertEqual(payload["queries"][0]["usage_min"], 0)
        self.assertEqual(payload["queries"][1]["usage_min"], 0)

    def test_load_error_exit_code(self):
        bad = self.dir / "bad.json"
        bad.write_text(json.dumps({"nodes": [{"id": 0, "parent": 7}]}))
        out = self.run_cli("--register", str(bad),
                           "--readings", str(self.rd_path),
                           "--period", "2024-01", "--nodes", "0", "--json")
        self.assertEqual(out.returncode, 1)
        err = json.loads(out.stderr)
        self.assertEqual(err["code"], "INPUT_INCOMPLETE")

    def test_out_file(self):
        out_path = self.dir / "result.json"
        out = self.run_cli("--register", str(self.reg_path),
                           "--readings", str(self.rd_path),
                           "--period", "2024-01", "--nodes", "1",
                           "--json", "--out", str(out_path))
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out_path.read_text())
        self.assertEqual(payload["queries"][0]["status"], "feasible")


class RunAuditEntryPoint(unittest.TestCase):
    def test_query_file_runner(self):
        root = Path(__file__).resolve().parents[1]
        out = subprocess.run(
            [sys.executable, str(root / "run_audit.py"),
             str(root / "examples" / "multi" / "query.json"), "--json"],
            capture_output=True, text=True, cwd=str(root))
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(payload["queries"]), 2)
        for q in payload["queries"]:
            self.assertIsNotNone(q["usage_min"])


if __name__ == "__main__":
    unittest.main()
