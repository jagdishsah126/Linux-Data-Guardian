"""Configuration semantics (regression tests for BUG-29/54)."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from src.data_guardian.config import ConfigManager, default_config_dir, default_data_dir
from tests.support import IsolatedTestCase


class TestConfigDefaults(IsolatedTestCase):
    def test_defaults(self):
        self.assertEqual(self.config.active_profile, "balanced")
        self.assertEqual(self.config.daily_limit_mb, 1000)
        self.assertEqual(self.config.monthly_limit_mb, 25000)
        self.assertEqual(self.config.billing_cycle_day, 1)
        self.assertEqual(self.config.warning_thresholds, [80, 95])
        self.assertTrue(self.config.enforce_quota)

    def test_invalid_profile_rejected(self):
        with self.assertRaises(ValueError):
            self.config.active_profile = "turbo"

    def test_network_profile_does_not_change_global_default(self):
        """BUG-29: per-network profiles must be independent of the global default."""
        self.config.set_network_profile("uuid-a", "strict")
        self.assertEqual(self.config.get_network_profile("uuid-a"), "strict")
        self.assertEqual(self.config.active_profile, "balanced")
        self.config.set_network_profile("uuid-b", "off")
        self.assertEqual(self.config.get_network_profile("uuid-a"), "strict")

    def test_network_profile_falls_back_to_global_default(self):
        self.config.active_profile = "off"
        self.assertEqual(self.config.get_network_profile("never-seen"), "off")

    def test_set_hotspot_writes_name_and_uuid(self):
        self.config.set_hotspot("uuid-1", "Example Wi-Fi")
        self.assertEqual(self.config.designated_hotspot_uuid, "uuid-1")
        self.assertEqual(self.config.designated_hotspot_name, "Example Wi-Fi")
        self.assertEqual(self.config.get_network_name("uuid-1"), "Example Wi-Fi")

    def test_config_file_is_private(self):
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

    def test_billing_cycle_day_clamped(self):
        self.config.config["quotas"]["billing_cycle_day"] = 31
        self.assertEqual(self.config.billing_cycle_day, 28)
        self.config.config["quotas"]["billing_cycle_day"] = 0
        self.assertEqual(self.config.billing_cycle_day, 1)

    def test_strict_whitelist_defaults(self):
        self.assertIn("opencode", self.config.strict_whitelist_processes)
        self.assertIn("antigravity", self.config.strict_whitelist_processes)

    def test_corrupt_numeric_values_do_not_crash(self):
        self.config.config["quotas"]["daily_limit_mb"] = "many"
        self.config.config["quotas"]["warning_thresholds"] = ["80", "nope", 200]
        self.assertEqual(self.config.daily_limit_mb, 1000)
        self.assertEqual(self.config.warning_thresholds, [80])

    def test_invalid_stored_profile_falls_back(self):
        self.config.config["app"]["default_profile"] = "turbo"
        self.assertEqual(self.config.active_profile, "balanced")

    def test_invalid_ports_are_dropped(self):
        self.config.whitelist["strict_allowed_ports_tcp"] = [443, 0, 70000, "x", 22]
        self.assertEqual(self.config.strict_allowed_ports_tcp, [22, 443])


class TestXdgSupport(IsolatedTestCase):
    def test_xdg_directories_are_honoured(self):
        """BUG-54: XDG_CONFIG_HOME / XDG_DATA_HOME must be respected."""
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg-cfg",
                                     "XDG_DATA_HOME": "/tmp/xdg-data"}):
            self.assertEqual(default_config_dir(), Path("/tmp/xdg-cfg/linux-data-guardian"))
            self.assertEqual(default_data_dir(), Path("/tmp/xdg-data/linux-data-guardian"))

    def test_fallback_without_xdg(self):
        env = dict(os.environ)
        env.pop("XDG_CONFIG_HOME", None)
        env.pop("XDG_DATA_HOME", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(default_config_dir(),
                             Path.home() / ".config" / "linux-data-guardian")


if __name__ == "__main__":
    unittest.main()
