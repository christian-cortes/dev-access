"""Pruebas de agregados horarios."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, stats, windows  # noqa: E402

CATALOG = {
    "settings": {"default_accept": [200]},
    "categories": [
        {"slug": "a", "title": "A"},
        {"slug": "b", "title": "B"},
    ],
    "services": [
        {"slug": "uno", "name": "Uno", "category": "a", "url": "https://uno.example"},
        {"slug": "dos", "name": "Dos", "category": "b", "url": "https://dos.example"},
    ],
}


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.configure(str(Path(self.tmp.name) / "portal.db"))
        db.init_db()
        db.sync_catalog(CATALOG)
        stats.hydrate_latencies()

    def tearDown(self):
        db.configure(None)
        self.tmp.cleanup()

    def test_empty_window_has_no_availability(self):
        payload = stats.compute("24h")
        self.assertIsNone(payload["summary"]["availability_pct"])
        self.assertEqual(payload["summary"]["coverage_pct"], 0.0)
        self.assertEqual(payload["summary"]["services_total"], 2)

    def test_mixed_checks_weight_availability(self):
        db.record_checks(
            [
                (1, 1, 200, 100, None, "online"),
                (1, 0, None, 200, "x", "offline"),
                (2, 1, 200, 300, None, "online"),
            ]
        )
        db.rollup_checks()
        stats.hydrate_latencies()
        payload = stats.compute("24h")
        self.assertEqual(payload["summary"]["availability_pct"], 66.7)
        self.assertEqual(payload["summary"]["coverage_pct"], 100.0)
        self.assertEqual(payload["summary"]["avg_latency_ms"], 200)
        self.assertEqual(payload["summary"]["p95_latency_ms"], 300)
        self.assertEqual(payload["series"][-1]["total"], 3)

    def test_invalid_window(self):
        with self.assertRaises(ValueError):
            windows.validate_window("1d")
        with self.assertRaises(ValueError):
            stats.compute("1d")


if __name__ == "__main__":
    unittest.main()
