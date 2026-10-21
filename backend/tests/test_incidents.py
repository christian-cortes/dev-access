"""Pruebas de incidentes y MTTR."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, incidents  # noqa: E402

CATALOG = {
    "settings": {"default_accept": [200]},
    "categories": [{"slug": "a", "title": "A"}],
    "services": [
        {"slug": "uno", "name": "Uno", "category": "a", "url": "https://uno.example"}
    ],
}


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.configure(str(Path(self.tmp.name) / "portal.db"))
        db.init_db()
        db.sync_catalog(CATALOG)

    def tearDown(self):
        db.configure(None)
        self.tmp.cleanup()

    def test_open_and_close_incident(self):
        now = "2026-10-21T10:00:00+00:00"
        later = "2026-10-21T10:05:00+00:00"
        db.apply_transition(
            {"uno": "offline"},
            {"uno": now},
            {"uno": 1},
            False,
            [("open_service", 1, now, None, "x", False)],
        )
        self.assertEqual(incidents.summary("24h")["incidents_open"], 1)
        db.apply_transition(
            {"uno": "online"},
            {"uno": later},
            {},
            False,
            [("close_service", 1, later)],
        )
        rows = incidents.list_incidents("7d")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["duration_s"], 300)
        self.assertEqual(incidents.summary("24h")["mttr_s"], 300)

    def test_ensure_open_incidents_is_idempotent(self):
        db.ensure_open_incidents({"uno": "offline"}, {})
        db.ensure_open_incidents({"uno": "offline"}, {})
        self.assertEqual(len(incidents.list_incidents("7d")), 1)

    def test_invalid_window(self):
        with self.assertRaises(ValueError):
            incidents.list_incidents("1d")


if __name__ == "__main__":
    unittest.main()
