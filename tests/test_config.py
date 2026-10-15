"""Unit tests for configuration management."""

import unittest
import tempfile
from pathlib import Path
from src.data_guardian.config import ConfigManager


class TestConfigManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "test_config.yaml"
        self.whitelist_path = Path(self.temp_dir.name) / "test_whitelist.yaml"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_config_loading(self):
        mgr = ConfigManager(self.config_path, self.whitelist_path)
        self.assertEqual(mgr.active_profile, "balanced")
        self.assertEqual(mgr.daily_limit_mb, 1000)
        self.assertIn("developer_cli", mgr.whitelist.get("whitelist", {}))

    def test_update_profile(self):
        mgr = ConfigManager(self.config_path, self.whitelist_path)
        mgr.active_profile = "strict"
        self.assertEqual(mgr.active_profile, "strict")

        # Reload from disk
        mgr2 = ConfigManager(self.config_path, self.whitelist_path)
        self.assertEqual(mgr2.active_profile, "strict")

    def test_invalid_profile_raises(self):
        mgr = ConfigManager(self.config_path, self.whitelist_path)
        with self.assertRaises(ValueError):
            mgr.active_profile = "invalid_mode"


if __name__ == "__main__":
    unittest.main()
