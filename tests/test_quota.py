import unittest
from datetime import datetime, timedelta, timezone

from quota import _get_status


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, passes, usage):
        self.passes = passes
        self.usage = usage

    def execute(self, query, _params):
        return Cursor(self.passes if "FROM plus_passes" in query else self.usage)


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def test_free_quota_exhausted_and_resets_after_24_hours(self):
        start = self.now - timedelta(hours=2)
        status, charge_id = _get_status(FakeConnection([], [(start, 5)]), 1, self.now)
        self.assertEqual(status.remaining, 0)
        self.assertFalse(status.is_plus)
        self.assertEqual(status.resets_at, start + timedelta(hours=24))
        self.assertIsNone(charge_id)

    def test_free_quota_resets_when_window_expires(self):
        status, _ = _get_status(FakeConnection([], [(self.now - timedelta(hours=24), 5)]), 1, self.now)
        self.assertEqual(status.remaining, 5)
        self.assertEqual(status.max_mb, 25)

    def test_plus_pass_has_30_file_limit(self):
        expires = self.now + timedelta(hours=10)
        status, charge_id = _get_status(FakeConnection([("charge", 29, expires)], []), 1, self.now)
        self.assertTrue(status.is_plus)
        self.assertEqual(status.remaining, 1)
        self.assertEqual(status.max_mb, 49)
        self.assertEqual(charge_id, "charge")

    def test_exhausted_plus_does_not_fall_back_to_free(self):
        expires = self.now + timedelta(hours=10)
        status, charge_id = _get_status(FakeConnection([("charge", 30, expires)], []), 1, self.now)
        self.assertTrue(status.is_plus)
        self.assertEqual(status.remaining, 0)
        self.assertIsNone(charge_id)


if __name__ == "__main__":
    unittest.main()
