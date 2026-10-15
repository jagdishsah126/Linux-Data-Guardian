"""Unit tests for SQLite database operations and quotas."""

import unittest
import tempfile
from pathlib import Path
from src.data_guardian.database import Database


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_guardian.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_schema_initialization(self):
        self.assertTrue(self.db_path.exists())
        with self.db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(version) FROM schema_version;")
            self.assertEqual(cursor.fetchone()[0], 2)

    def test_session_lifecycle(self):
        session_id = self.db.start_session("uuid-1234", "NTC Hotspot", "wlp0s20f3", "balanced")
        self.assertGreater(session_id, 0)

        self.db.update_session(session_id, delta_rx=1024, delta_tx=512)
        with self.db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT rx_bytes, tx_bytes, ended_at FROM sessions WHERE id = ?;", (session_id,))
            row = cursor.fetchone()
            self.assertEqual(row["rx_bytes"], 1024)
            self.assertEqual(row["tx_bytes"], 512)
            self.assertIsNone(row["ended_at"])

        self.db.end_session(session_id)
        with self.db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT ended_at FROM sessions WHERE id = ?;", (session_id,))
            row = cursor.fetchone()
            self.assertIsNotNone(row["ended_at"])

    def test_daily_usage_and_quota_exceeded(self):
        # 10 MB quota limit (10 * 1024 * 1024 = 10,485,760 bytes)
        quota_bytes = 10 * 1024 * 1024

        # Add 6 MB usage -> not exceeded
        res1 = self.db.record_usage_delta(delta_rx=4 * 1024 * 1024, delta_tx=2 * 1024 * 1024, quota_limit_bytes=quota_bytes)
        self.assertFalse(res1["quota_exceeded"])
        self.assertEqual(res1["total_bytes"], 6 * 1024 * 1024)

        # Add another 5 MB usage -> total 11 MB -> exceeded
        res2 = self.db.record_usage_delta(delta_rx=5 * 1024 * 1024, delta_tx=0, quota_limit_bytes=quota_bytes)
        self.assertTrue(res2["quota_exceeded"])
        self.assertEqual(res2["total_bytes"], 11 * 1024 * 1024)

        # Grant temporary allowance of 5 MB (total effective limit: 15 MB) -> unblocks
        self.db.grant_temporary_allowance(5 * 1024 * 1024)
        usage = self.db.get_today_usage()
        self.assertFalse(usage["quota_exceeded"])
        self.assertEqual(usage["effective_limit_bytes"], 15 * 1024 * 1024)

    def test_approval_events_logging(self):
        event_id = self.db.record_approval_event(
            tool="apt",
            packages="curl, wget",
            estimated_bytes=2500000,
            decision="approved"
        )
        self.assertGreater(event_id, 0)

        recent = self.db.get_recent_approvals(limit=5)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["tool"], "apt")
        self.assertEqual(recent[0]["decision"], "approved")

    def test_monthly_usage_and_per_network_tracking(self):
        # Record delta for specific network
        res = self.db.record_usage_delta(
            delta_rx=1024 * 1024,
            delta_tx=512 * 1024,
            connection_uuid="uuid-hotspot-test",
            connection_name="Test Hotspot"
        )
        self.assertGreaterEqual(res["total_bytes"], 1536 * 1024)

        # Query per-network today usage
        net_today = self.db.get_network_today_usage("uuid-hotspot-test")
        self.assertEqual(net_today["connection_name"], "Test Hotspot")
        self.assertEqual(net_today["rx_bytes"], 1024 * 1024)
        self.assertEqual(net_today["tx_bytes"], 512 * 1024)

        # Query monthly usage
        monthly = self.db.get_monthly_usage(connection_uuid="uuid-hotspot-test", billing_cycle_day=1)
        self.assertEqual(monthly["total_bytes"], 1536 * 1024)


if __name__ == "__main__":
    unittest.main()
