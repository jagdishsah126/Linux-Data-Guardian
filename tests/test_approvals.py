"""Unit tests for approval gate and shims."""

import unittest
import tempfile
from pathlib import Path
from src.data_guardian.config import ConfigManager
from src.data_guardian.approvals.gate import ApprovalGate
from src.data_guardian.approvals.shims import install_shims, remove_shims


class TestApprovals(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "test_config.yaml"
        self.whitelist_path = Path(self.temp_dir.name) / "test_whitelist.yaml"
        self.config_mgr = ConfigManager(self.config_path, self.whitelist_path)
        self.gate = ApprovalGate(self.config_mgr)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_is_download_action_detection(self):
        # pip
        self.assertTrue(self.gate.is_download_action("pip", ["install", "fastapi"]))
        self.assertTrue(self.gate.is_download_action("pip", ["download", "wheel"]))
        self.assertFalse(self.gate.is_download_action("pip", ["list"]))
        self.assertFalse(self.gate.is_download_action("pip", ["show", "requests"]))

        # npm
        self.assertTrue(self.gate.is_download_action("npm", ["install", "lodash"]))
        self.assertTrue(self.gate.is_download_action("npm", ["i", "react"]))
        self.assertTrue(self.gate.is_download_action("npm", ["add", "vue"]))
        self.assertFalse(self.gate.is_download_action("npm", ["run", "build"]))
        self.assertFalse(self.gate.is_download_action("npm", ["--version"]))

        # apt
        self.assertTrue(self.gate.is_download_action("apt", ["install", "htop"]))
        self.assertTrue(self.gate.is_download_action("apt", ["upgrade"]))
        self.assertTrue(self.gate.is_download_action("apt", ["update"]))

        # cargo
        self.assertTrue(self.gate.is_download_action("cargo", ["install", "ripgrep"]))
        self.assertFalse(self.gate.is_download_action("cargo", ["check"]))

    def test_profile_off_bypasses_gating(self):
        self.config_mgr.active_profile = "off"
        should_gate, reason = self.gate.should_gate("pip", ["install", "requests"])
        self.assertFalse(should_gate)
        self.assertIn("off", reason)

    def test_shim_installation_and_removal(self):
        shim_dir = Path(self.temp_dir.name) / "bin"
        installed = install_shims(target_dir=shim_dir)
        self.assertEqual(len(installed), 4)

        for path in installed:
            self.assertTrue(path.exists())
            self.assertTrue(path.is_file())
            content = path.read_text()
            self.assertIn("Linux Data Guardian", content)

        remove_shims(target_dir=shim_dir)
        for path in installed:
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
