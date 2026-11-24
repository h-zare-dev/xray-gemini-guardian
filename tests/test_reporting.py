"""Regression checks: a failed port among successes must remain visible."""
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from guardian.reporting import (bucket, filtered, full_report_chunks,
                                progress_message, summary)
from guardian.storage import Store


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "guardian.sqlite3")
        self.targets = [
            SimpleNamespace(name=f"VLESS_{port}", port=port)
            for port in range(21001, 21006)
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_mixed_results_and_pending_ports_are_not_hidden(self):
        run_id = self.store.begin_run(5, self.targets)
        for sequence, status in enumerate(
            ("WEB_OK", "REGION_BLOCKED", "WEB_OK"), 1
        ):
            self.store.add_result(
                run_id, sequence, self.targets[sequence - 1].name,
                self.targets[sequence - 1].port, status,
                "Evidence", 1, "",
            )
        rows = self.store.run_entries(run_id)
        self.assertEqual(
            [r["status"] for r in rows],
            ["WEB_OK", "REGION_BLOCKED", "WEB_OK", "NOT_TESTED", "NOT_TESTED"],
        )
        stats = summary(self.store.run(run_id), rows)
        self.assertEqual((stats.ok, stats.blocked, stats.review, stats.pending),
                         (2, 1, 0, 2))
        self.assertEqual([r["port"] for r in filtered(rows, "blocked")], [21002])
        self.assertEqual(len(filtered(rows, "ok")), 2)
        self.assertEqual(len(filtered(rows, "pending")), 2)
        self.assertEqual(sum(chunk.count("21002") for chunk in full_report_chunks(rows, 90)), 1)

    def test_progress_alert_after_each_port_with_counts(self):
        result = SimpleNamespace(
            status="REGION_BLOCKED", detail="Unsupported region", attempts=2,
        )
        counts = Counter({"ok": 1, "blocked": 1})
        msg = progress_message(2, self.targets[1], result, counts, 5)
        self.assertIn("[2/5 | 40%]", msg)
        self.assertIn("21002", msg)
        self.assertIn("REGION_BLOCKED", msg)
        self.assertIn("🚫 منطقه‌ای: 1", msg)
        self.assertIn("⏳ مانده: 3", msg)

    def test_account_problem_not_classified_as_bad_ip(self):
        self.assertEqual(bucket("ACCOUNT_RESTRICTED"), "review")
        self.assertEqual(bucket("LOGIN_REQUIRED"), "review")
        self.assertEqual(bucket("TEST_ERROR"), "review")
        self.assertEqual(bucket("WEB_OK"), "ok")
        self.assertEqual(bucket("REGION_BLOCKED"), "blocked")

    def test_telegram_full_report_chunks_preserve_every_port(self):
        rows = [
            {"port": 20000 + i, "status": "WEB_OK" if i % 5 else "TEST_ERROR",
             "attempts": 1}
            for i in range(1000)
        ]
        chunks = full_report_chunks(rows)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 3000 for chunk in chunks))
        joined = "\n".join(chunks)
        self.assertEqual(joined.count("TEST_ERROR"), 200)
        for i in range(1000):
            self.assertIn(str(20000 + i), joined)

    def test_legacy_run_without_manifest(self):
        run_id = self.store.begin_run(3)
        self.store.add_result(run_id, 1, "old", 21001, "WEB_OK", "ok", 1, "")
        rows = self.store.run_entries(run_id)
        self.assertEqual(len(rows), 1)
        stats = summary(self.store.run(run_id), rows)
        self.assertEqual(stats.pending, 2)


if __name__ == "__main__":
    unittest.main()
