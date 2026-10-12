"""Pruebas de la histéresis de estado (stdlib unittest, sin dependencias externas).

Ejecutar desde `backend/`:  python -m unittest discover -s tests
"""
import os
import sys
import unittest

os.environ["PORTAL_FAIL_THRESHOLD"] = "3"
os.environ["PORTAL_OK_THRESHOLD"] = "2"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import status  # noqa: E402


class AdvanceTests(unittest.TestCase):
    def test_single_failure_keeps_online(self):
        self.assertEqual(status.advance("online", [False], "offline"), "online")

    def test_two_failures_keep_online(self):
        self.assertEqual(status.advance("online", [False, False], "offline"), "online")

    def test_fail_threshold_marks_offline(self):
        self.assertEqual(status.advance("online", [False, False, False], "offline"), "offline")

    def test_stays_offline_after_one_success(self):
        self.assertEqual(status.advance("offline", [True], "online"), "offline")

    def test_recovers_after_ok_threshold(self):
        self.assertEqual(status.advance("offline", [True, True], "online"), "online")

    def test_recovers_to_auth_state(self):
        self.assertEqual(status.advance("offline", [True, True], "auth"), "auth")

    def test_unknown_initial_down_needs_threshold(self):
        self.assertEqual(status.advance("unknown", [False], "offline"), "unknown")
        self.assertEqual(status.advance("unknown", [False, False, False], "offline"), "offline")

    def test_unknown_initial_up(self):
        self.assertEqual(status.advance("unknown", [True], "online"), "online")

    def test_invalid_committed_treated_unknown(self):
        self.assertEqual(status.advance("garbage", [True], "online"), "online")


if __name__ == "__main__":
    unittest.main()
