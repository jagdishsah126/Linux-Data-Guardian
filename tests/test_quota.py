"""Quota engine and database semantics (regression tests for BUG-01/02/06/07/08)."""

import unittest

from src.data_guardian.quota import evaluate, newly_fired_thresholds
from tests.support import MB, IsolatedTestCase


class TestQuotaOnEmptyDatabase(IsolatedTestCase):
    def test_empty_day_is_not_blocked(self):
        """BUG-01: an empty day must never look 'already exceeded'."""
        verdict = evaluate(self.db, self.config)
        self.assertEqual(verdict.daily_used, 0)
        self.assertIsNone(verdict.reason)
        self.assertFalse(verdict.blocked)
        self.assertEqual(verdict.level, "ok")

    def test_zero_daily_limit_means_unlimited(self):
        self.config.config["quotas"]["daily_limit_mb"] = 0
        self.record(5000, metered=True, limit_mb=0)
        verdict = evaluate(self.db, self.config)
        self.assertIsNone(verdict.reason)
        self.assertFalse(verdict.blocked)

    def test_grant_allowance_on_empty_day_persists(self):
        """BUG-02: granting before any traffic must work."""
        self.db.grant_temporary_allowance(100 * MB)
        state = self.db.get_quota_state()
        self.assertEqual(state["allowance_bytes"], 100 * MB)
        self.assertEqual(evaluate(self.db, self.config).daily_effective, 1100 * MB)

    def test_grant_allowance_rejects_non_positive(self):
        with self.assertRaises(ValueError):
            self.db.grant_temporary_allowance(0)
        with self.assertRaises(ValueError):
            self.db.grant_temporary_allowance(-5)

    def test_allowance_clears_block(self):
        self.record(1000, metered=True)
        self.assertTrue(evaluate(self.db, self.config).blocked)
        self.db.grant_temporary_allowance(50 * MB)
        verdict = evaluate(self.db, self.config)
        self.assertFalse(verdict.blocked)
        self.assertEqual(self.db.get_quota_state()["exceeded"], 0)


class TestMeteredAccounting(IsolatedTestCase):
    def test_unmetered_traffic_does_not_consume_quota(self):
        """BUG-06: home Wi-Fi usage must not burn the hotspot budget."""
        self.record(5000, metered=False, uuid="home", name="Home")
        verdict = evaluate(self.db, self.config)
        self.assertEqual(verdict.daily_used, 0)
        self.assertFalse(verdict.blocked)
        # Statistics still record it.
        self.assertEqual(self.db.get_today_usage()["total_bytes"], 5000 * MB)

    def test_metered_and_unmetered_are_separate(self):
        self.record(300, metered=False, uuid="home", name="Home")
        self.record(200, metered=True)
        verdict = evaluate(self.db, self.config)
        self.assertEqual(verdict.daily_used, 200 * MB)
        self.assertEqual(self.db.get_today_usage()["total_bytes"], 500 * MB)

    def test_monthly_counts_only_metered(self):
        self.record(900, metered=False, uuid="home", name="Home")
        self.record(100, metered=True)
        self.assertEqual(self.db.get_monthly_usage()["total_bytes"], 100 * MB)

    def test_metered_flag_survives_designation_change(self):
        """A row written while metered stays metered even if the hotspot changes."""
        self.record(100, metered=True, uuid="old-hotspot")
        new_db = type(self.db)(self.db_path, designated_uuid="new-hotspot")
        self.assertEqual(new_db.metered_total(), 100 * MB)


class TestMonthlyAndWarnings(IsolatedTestCase):
    def test_monthly_enforced_when_daily_is_unlimited(self):
        """BUG-07: monthly-only plans must work."""
        self.config.config["quotas"]["daily_limit_mb"] = 0
        self.config.config["quotas"]["monthly_limit_mb"] = 100
        self.record(150, metered=True, limit_mb=0)
        verdict = evaluate(self.db, self.config)
        self.assertEqual(verdict.reason, "monthly")
        self.assertTrue(verdict.blocked)

    def test_warn_only_never_blocks(self):
        """BUG-08: warn_only reports the level but does not enforce."""
        self.config.config["quotas"]["action_on_exhaustion"] = "warn_only"
        self.record(1200, metered=True)
        verdict = evaluate(self.db, self.config)
        self.assertEqual(verdict.level, "exceeded")
        self.assertFalse(verdict.enforce)
        self.assertFalse(verdict.blocked)

    def test_thresholds_fire_once(self):
        self.record(810, metered=True)
        verdict = evaluate(self.db, self.config)
        self.assertEqual(verdict.level, "warning")
        pending = newly_fired_thresholds(self.db, self.config, verdict)
        self.assertEqual(pending, [80])
        self.assertTrue(self.db.mark_warning_fired(80))
        self.assertFalse(self.db.mark_warning_fired(80))
        self.assertEqual(newly_fired_thresholds(self.db, self.config, verdict), [])

    def test_both_thresholds_available(self):
        self.assertEqual(self.config.warning_thresholds, [80, 95])


class TestDatabaseHardening(IsolatedTestCase):
    def test_database_permissions_are_private(self):
        """BUG-17: usage history must not be world-readable."""
        mode = self.db_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.db_path.parent.stat().st_mode & 0o777, 0o700)

    def test_orphan_sessions_are_closed(self):
        """BUG-15: a crash must not leave open sessions forever."""
        session_id = self.db.start_session("uuid", "Name", "wlan0")
        self.assertEqual(self.db.close_orphan_sessions(), 1)
        with self.db._connection() as conn:
            row = conn.execute("SELECT ended_at FROM sessions WHERE id = ?;", (session_id,)).fetchone()
        self.assertIsNotNone(row["ended_at"])

    def test_schema_version_is_current(self):
        with self.db._connection() as conn:
            version = conn.execute("SELECT MAX(version) FROM schema_version;").fetchone()[0]
        self.assertEqual(version, 3)

    def test_history_window_is_exact(self):
        """BUG-45: `days=60` must not include 61 days."""
        from datetime import date, timedelta

        today = date.today()
        with self.db._connection() as conn:
            for offset in (0, 59, 60):
                day = (today - timedelta(days=offset)).isoformat()
                conn.execute(
                    "INSERT INTO network_daily_usage "
                    "(date, connection_uuid, connection_name, rx_bytes, tx_bytes, metered, updated_at) "
                    "VALUES (?, 'u', 'n', 1, 0, 1, datetime('now'));",
                    (day,),
                )
            conn.commit()
        history = self.db.get_all_networks_history(days=60)
        dates = {row["date"] for row in history["u"]}
        self.assertEqual(len(dates), 2)  # today and today-59; today-60 excluded


if __name__ == "__main__":
    unittest.main()
