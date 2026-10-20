"""Approval gate classification and shim behaviour (regression tests for BUG-11/32)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.data_guardian.approvals.gate import ApprovalGate, find_subcommand
from src.data_guardian.approvals.shims import (
    SUPPORTED_TOOLS,
    install_shims,
    remove_shims,
)
from tests.support import IsolatedTestCase


class TestDownloadClassification(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.gate = ApprovalGate(self.config)

    def test_read_only_commands_are_not_downloads(self):
        """BUG-11: `pip3 list` must never be gated."""
        cases = [
            ("pip", ["list"]),
            ("pip3", ["list"]),
            ("pip3", ["--version"]),
            ("pip", ["show", "requests"]),
            ("pip", ["freeze"]),
            ("npm", ["run", "build"]),
            ("npm", ["--version"]),
            ("cargo", ["build"]),
            ("cargo", ["--version"]),
            ("apt", ["list", "--installed"]),
            ("apt", ["show", "curl"]),
        ]
        for tool, args in cases:
            self.assertFalse(self.gate.is_download_action(tool, args), f"{tool} {args}")

    def test_download_commands_are_detected(self):
        cases = [
            ("pip", ["install", "requests"]),
            ("pip3", ["install", "requests"]),
            ("pip", ["download", "numpy"]),
            ("pip3", ["wheel", "numpy"]),
            ("npm", ["install"]),
            ("npm", ["ci"]),
            ("npm", ["add", "vue"]),
            ("cargo", ["install", "ripgrep"]),
            ("apt", ["install", "htop"]),
            ("apt-get", ["upgrade"]),
            ("apt", ["update"]),
            ("python3", ["-m", "pip", "install", "x"]),
            ("python", ["-m", "pip", "download", "x"]),
        ]
        for tool, args in cases:
            self.assertTrue(self.gate.is_download_action(tool, args), f"{tool} {args}")

    def test_flags_with_values_are_skipped(self):
        self.assertFalse(self.gate.is_download_action("pip3", ["-c", "install.cfg", "list"]))
        self.assertTrue(self.gate.is_download_action("pip3", ["-c", "install.cfg", "install", "x"]))
        self.assertTrue(self.gate.is_download_action("pip3", ["install", "-r", "requirements.txt"]))

    def test_unknown_tool_is_not_gated(self):
        self.assertFalse(self.gate.is_download_action("htop", ["anything"]))

    def test_find_subcommand(self):
        self.assertEqual(find_subcommand("pip", ["install", "x"]), "install")
        self.assertEqual(find_subcommand("pip", ["--quiet", "install", "x"]), "install")
        self.assertEqual(find_subcommand("python3", ["-m", "pip", "install", "x"]), "install")
        self.assertIsNone(find_subcommand("pip", ["--version"]))

    def test_should_gate_off_profile(self):
        self.config.active_profile = "off"
        should, _ = self.gate.should_gate("pip", ["install", "x"])
        self.assertFalse(should)

    def test_per_network_off_profile_disables_gating(self):
        """The hotspot's saved profile wins over the global default."""
        from types import SimpleNamespace

        active = SimpleNamespace(uuid="hot-uuid", is_metered=True)
        self.config.set_network_profile("hot-uuid", "off")
        self.gate.monitor.get_active_connection = lambda: active
        self.gate.monitor.matches_designated = lambda conn: True
        should, reason = self.gate.should_gate("pip", ["install", "x"])
        self.assertFalse(should)
        self.assertIn("off", reason)


class TestShims(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.bin_dir = self.tmp_root / "bin"
        self.home = self.tmp_root / "home"
        self.home.mkdir()
        self.config_dir = self.tmp_root / "config"

    def test_install_and_remove(self):
        installed = install_shims(self.bin_dir, self.config_dir)
        self.assertEqual({p.name for p in installed}, set(SUPPORTED_TOOLS))
        for path in installed:
            self.assertTrue(os.access(path, os.X_OK), path)
            content = path.read_text()
            self.assertIn("Linux Data Guardian", content)
            self.assertIn("approvals.gate", content)

        removed = remove_shims(self.bin_dir, self.config_dir)
        self.assertEqual(len(removed), len(SUPPORTED_TOOLS))
        self.assertFalse(any(p.exists() for p in installed))

    def test_remove_leaves_foreign_binaries_alone(self):
        foreign = self.bin_dir / "npm"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        foreign.write_text("#!/bin/sh\necho real npm\n")
        remove_shims(self.bin_dir, self.config_dir)
        self.assertTrue(foreign.exists())

    def test_shim_passes_through_when_gate_allows(self):
        """A non-download command must reach the real binary."""
        install_shims(self.bin_dir, self.config_dir)
        real_dir = self.tmp_root / "real"
        real_dir.mkdir()
        real = real_dir / "pip"
        real.write_text("#!/bin/sh\necho REAL-PIP \"$@\"\n")
        real.chmod(0o755)

        # Gate always allows (daemon not on hotspot) -> shim must exec the real pip.
        env = {
            "PATH": f"{self.bin_dir}:{real_dir}:/usr/bin:/bin",
            "HOME": str(self.home),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        }
        proc = subprocess.run(
            [str(self.bin_dir / "pip"), "list"],
            env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REAL-PIP list", proc.stdout)

    def test_shim_fails_closed_for_downloads_when_gate_missing(self):
        """If the project disappeared, downloads are refused but reads still work."""
        install_shims(self.bin_dir, self.config_dir)
        shim = self.bin_dir / "pip"
        content = shim.read_text().replace(
            str(Path(__file__).resolve().parent.parent), "/nonexistent/project"
        )
        shim.write_text(content)

        env = {"PATH": f"{self.bin_dir}:/usr/bin:/bin", "HOME": str(self.home)}
        proc = subprocess.run([str(shim), "install", "requests"],
                              env=env, text=True, capture_output=True, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("approval gate unavailable", proc.stderr)


if __name__ == "__main__":
    unittest.main()
