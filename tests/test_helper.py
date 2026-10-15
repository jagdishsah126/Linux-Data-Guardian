"""Tests for the data-guardian-helper argument validation."""

import os
import subprocess
import unittest
from pathlib import Path

HELPER_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "data-guardian-helper"


class TestHelperValidation(unittest.TestCase):
    def setUp(self):
        self.assertTrue(HELPER_SCRIPT.exists(), "Helper script must exist")

    def test_missing_action_fails(self):
        # Running without args or as non-root should fail cleanly
        res = subprocess.run([str(HELPER_SCRIPT)], capture_output=True, text=True)
        # Expect exit code 1 (not root) or 2 (invalid action)
        self.assertIn(res.returncode, (1, 2))

    def test_invalid_interface_rejected(self):
        # Invalid interface strings (command injection attempts or bad chars)
        res = subprocess.run(
            [str(HELPER_SCRIPT), "--apply-balanced", "wlan0; rm -rf /"],
            capture_output=True,
            text=True
        )
        self.assertIn(res.returncode, (1, 2))


if __name__ == "__main__":
    unittest.main()
