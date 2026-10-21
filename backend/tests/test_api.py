"""Pruebas de contrato HTTP con una BD y catálogo temporales."""
import json
import os
import sys
import tempfile
import unittest
import asyncio
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["PORTAL_DISABLE_POLL"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app import main  # noqa: E402

CATALOG = {
    "settings": {"default_accept": [200]},
    "categories": [{"slug": "cat", "title": "Cat", "description": "<b>Cat</b>"}],
    "services": [
        {
            "slug": "uno",
            "name": "Uno",
            "category": "cat",
            "url": "https://uno.example",
            "note": '<script>alert(1)</script><a href="javascript:alert(2)">x</a>',
        }
    ],
}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.configure(str(Path(self.tmp.name) / "portal.db"))
        self.catalog_path = Path(self.tmp.name) / "catalog.yml"
        self.catalog_path.write_text(json.dumps(CATALOG), encoding="utf-8")
        main.db.CATALOG_PATH = str(self.catalog_path)
        main.state.stats_cache.clear()
        self.original_probe = main.probe.probe_all
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        main.probe.probe_all = self.original_probe
        self.client.__exit__(None, None, None)
        db.configure(None)
        self.tmp.cleanup()

    def test_status_contract(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        status = body["statuses"]["uno"]
        self.assertIn("state", status)
        self.assertIn("last_state", status)
        self.assertIn("state_since", status)
        self.assertIn("uptime_24h_pct", status)
        self.assertEqual(body["refresh_ms"], 30000)

    def test_catalog_is_sanitized_and_has_etag(self):
        response = self.client.get("/api/catalog")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script", response.text)
        self.assertNotIn("javascript:", response.text)
        etag = response.headers["etag"]
        cached = self.client.get("/api/catalog", headers={"If-None-Match": etag})
        self.assertEqual(cached.status_code, 304)

    def test_stats_and_incidents(self):
        self.assertEqual(self.client.get("/api/stats?window=24h").status_code, 200)
        self.assertEqual(self.client.get("/api/stats?window=nope").status_code, 422)
        self.assertEqual(self.client.get("/api/incidents?window=7d").status_code, 200)

    def test_no_store_header(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_failed_checks_commit_offline_and_open_incidents(self):
        async def fake(services):
            return [
                {
                    "ok": 0,
                    "state": "offline",
                    "code": None,
                    "latency_ms": 1,
                    "error": "boom",
                }
                for _ in services
            ]

        main.probe.probe_all = fake
        for _ in range(3):
            self.assertTrue(asyncio.run(main.run_checks()))
        body = self.client.get("/api/status").json()
        self.assertEqual(body["statuses"]["uno"]["state"], "offline")
        self.assertEqual(body["statuses"]["uno"]["last_state"], "offline")
        incidents_body = self.client.get("/api/incidents?window=7d").json()
        self.assertTrue(incidents_body["incidents"])


if __name__ == "__main__":
    unittest.main()
