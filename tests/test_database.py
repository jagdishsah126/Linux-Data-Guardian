"""Unit tests for SQLite statistics, quota bookkeeping and notifications."""

import os
import tempfile
import unittest
from pathlib import Path

from src.data_guardian.database import CURRENT_SCHEMA_VERSION, Database

MB = 1024 * 1024


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_guardian.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_schema_initialization(self):
        self.assertTrue(self.db_path.exists())
        with self.db._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(version) FROM schema_version;")
            self.assertEqual(cursor.fetchone()[0], CURRENT_SCHEMA_VERSION)
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
            )
            tables = {row["name"] for row in cursor.fetchall()}
        for table in ("daily_usage", "network_daily_usage", "quota_state",
                      "sessions", "notifications", "approval_events"):
            self.assertIn(table, tables)

    def test_database_permissions_are_private(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores mode bits")
        self.assertEqual(self.db_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.db_path.parent.stat().st_mode & 0o777, 0o700)

    def test_short_lived_connections_are_closed(self):
        """Every query must close its SQLite handle (fd exhaustion regression)."""
        if not Path("/proc/self/fd").is_dir():
            self.skipTest("/proc is not available")
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(60):
            self.db.get_today_usage()
            self.db.latest_notification_id()
        after = len(os.listdir("/proc/self/fd"))
        self.assertLessEqual(after, before + 5, f"fd count grew from {before} to {after}")

    def test_connection_is_closed_at_context_exit(self):
        import sqlite3

        with self.db._connection() as conn:
            conn.execute("SELECT 1;")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1;")

    def test_session_lifecycle(self):
        session_id = self.db.start_session("uuid-1234", "Example Hotspot", "wlan0", "balanced")
        self.assertGreater(session_id, 0)

        self.db.update_session(session_id, delta_rx=1024, delta_tx=512)
        with self.db._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT rx_bytes, tx_bytes, ended_at FROM sessions WHERE id = ?;",
                (session_id,),
            )
            row = cursor.fetchone()
            self.assertEqual(row["rx_bytes"], 1024)
            self.assertEqual(row["tx_bytes"], 512)
            self.assertIsNone(row["ended_at"])

        self.db.end_session(session_id)
        with self.db._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT ended_at FROM sessions WHERE id = ?;", (session_id,))
            self.assertIsNotNone(cursor.fetchone()["ended_at"])

    def test_daily_usage_and_quota_bookkeeping(self):
        quota_bytes = 10 * MB

        first = self.db.record_usage_delta(
            delta_rx=4 * MB, delta_tx=2 * MB,
            metered=True, daily_limit_bytes=quota_bytes,
        )
        self.assertEqual(first["total_bytes"], 6 * MB)
        self.assertEqual(first["effective_limit_bytes"], quota_bytes)

        second = self.db.record_usage_delta(
            delta_rx=5 * MB, delta_tx=0,
            metered=True, daily_limit_bytes=quota_bytes,
        )
        self.assertEqual(second["total_bytes"], 11 * MB)

        # Recording traffic never flips the block flag by itself: the quota
        # engine owns that decision (single writer).
        self.assertFalse(second["quota_exceeded"])

        self.db.set_quota_exceeded(True, "daily")
        self.assertTrue(self.db.get_today_usage()["quota_exceeded"])

        self.db.grant_temporary_allowance(5 * MB)
        usage = self.db.get_today_usage()
        self.assertFalse(usage["quota_exceeded"])
        self.assertEqual(usage["effective_limit_bytes"], 15 * MB)
        self.assertEqual(usage["temporary_allowance_bytes"], 5 * MB)

    def test_zero_limit_means_unlimited(self):
        self.db.set_daily_limit(0)
        usage = self.db.get_today_usage()
        self.assertEqual(usage["effective_limit_bytes"], 0)

    def test_warning_threshold_recorded_once(self):
        self.assertTrue(self.db.mark_warning_fired(80))
        self.assertFalse(self.db.mark_warning_fired(80))
        self.assertTrue(self.db.mark_warning_fired(95))
        self.assertEqual(self.db.get_today_usage()["warnings_fired"], [80, 95])

    def test_approval_events_logging(self):
        event_id = self.db.record_approval_event(
            tool="apt",
            packages="curl, wget",
            estimated_bytes=2500000,
            decision="approved",
        )
        self.assertGreater(event_id, 0)

        recent = self.db.get_recent_approvals(limit=5)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["tool"], "apt")
        self.assertEqual(recent[0]["decision"], "approved")

    def test_monthly_usage_counts_only_metered(self):
        self.db.record_usage_delta(
            delta_rx=1 * MB, delta_tx=512 * 1024,
            metered=True,
            connection_uuid="uuid-hotspot-test",
            connection_name="Test Hotspot",
        )
        # Unmetered traffic must not consume the metered quota.
        self.db.record_usage_delta(
            delta_rx=8 * MB, delta_tx=0,
            metered=False,
            connection_uuid="uuid-home",
            connection_name="Home Wi-Fi",
        )

        net_today = self.db.get_network_today_usage("uuid-hotspot-test")
        self.assertEqual(net_today["connection_name"], "Test Hotspot")
        self.assertEqual(net_today["rx_bytes"], 1 * MB)
        self.assertEqual(net_today["tx_bytes"], 512 * 1024)
        self.assertTrue(net_today["metered"])

        monthly = self.db.get_monthly_usage(billing_cycle_day=1)
        self.assertEqual(monthly["total_bytes"], MB + 512 * 1024)

    def test_metered_flag_is_frozen_per_row(self):
        """A network that was metered stays metered after its designation changes."""
        self.db.record_usage_delta(
            delta_rx=MB, delta_tx=0, metered=True,
            connection_uuid="uuid-hotspot-test", connection_name="Hotspot",
        )
        self.db.record_usage_delta(
            delta_rx=MB, delta_tx=0, metered=False,
            connection_uuid="uuid-hotspot-test", connection_name="Hotspot",
        )
        self.assertTrue(self.db.get_network_today_usage("uuid-hotspot-test")["metered"])
        self.assertEqual(self.db.metered_total(), 2 * MB)


if __name__ == "__main__":
    unittest.main()
