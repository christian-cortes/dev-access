"""Pruebas de esquema, estado y rollup con una BD temporal."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402

CATALOG = {
    "settings": {"default_accept": [200]},
    "categories": [{"slug": "cat", "title": "Cat"}],
    "services": [
        {"slug": "uno", "name": "Uno", "category": "cat", "url": "https://uno.example"},
        {"slug": "dos", "name": "Dos", "category": "cat", "url": "https://dos.example"},
    ],
}


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "portal.db")
        db.configure(self.path)
        db.init_db()
        db.sync_catalog(CATALOG)

    def tearDown(self):
        db.configure(None)
        self.tmp.cleanup()

    def test_migration_is_idempotent(self):
        db.init_db()
        self.assertEqual(db.schema_version(), db.SCHEMA_VERSION)

    def test_uptime_is_percentage(self):
        db.record_checks([(1, 1, 200, 10, None, "online")])
        status = db.latest_samples()["uno"]
        self.assertEqual(status["uptime_24h_pct"], 100.0)
        self.assertEqual(status["uptime_24h"], 100.0)

    def test_committed_state_overrides_raw(self):
        db.record_checks([(1, 0, None, 20, "boom", "offline")])
        db.set_meta("state_committed", '{"uno": "online"}')
        db.set_meta("state_since", '{"uno": "2026-10-21T00:00:00+00:00"}')
        status = db.current_statuses()["uno"]
        self.assertEqual(status["state"], "online")
        self.assertEqual(status["last_state"], "offline")
        self.assertEqual(status["state_since"], "2026-10-21T00:00:00+00:00")

    def test_recent_oks_is_newest_first(self):
        db.record_checks(
            [
                (1, 1, 200, 1, None, "online"),
                (1, 0, None, 2, "x", "offline"),
                (1, 1, 200, 3, None, "online"),
            ]
        )
        self.assertEqual(db.recent_oks(2)["uno"], [True, False])

    def test_rollup_is_idempotent(self):
        db.record_checks(
            [
                (1, 1, 200, 10, None, "online"),
                (1, 0, None, 20, "x", "offline"),
                (2, 1, 200, 30, None, "online"),
            ]
        )
        db.rollup_checks()
        db.rollup_checks()
        with db.db() as conn:
            total = conn.execute("SELECT SUM(total) AS n FROM checks_hourly").fetchone()["n"]
        self.assertEqual(total, 3)

    def test_purge_respects_retention(self):
        db.record_checks([(1, 1, 200, 10, None, "online")])
        self.assertTrue(db.purge_old_checks(force=True))
        self.assertFalse(db.purge_old_checks())


if __name__ == "__main__":
    unittest.main()
