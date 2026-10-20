"""Diagnostics, CLI wiring and installer safety (regression tests for BUG-03/18/20/24/27)."""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from src.data_guardian import main as cli
from src.data_guardian.diagnostics import CheckResult, Doctor, format_report
from tests.support import IsolatedTestCase

ROOT = Path(__file__).resolve().parent.parent


class TestDoctorWiring(unittest.TestCase):
    def test_doctor_subcommand_exists(self):
        """BUG-24: `doctor` must be reachable from the CLI."""
        parser = cli.build_parser()
        args = parser.parse_args(["doctor", "--json"])
        self.assertEqual(args.command, "doctor")
        self.assertTrue(args.json)

    def test_doctor_checks_cover_core_areas(self):
        names = {r.name for r in Doctor().run_all()}
        for expected in ("Privileged helper", "Sudoers rule", "Background daemon",
                         "SQLite database", "nftables", "Strict-mode cgroups"):
            self.assertIn(expected, names)

    def test_report_rendering(self):
        results = [
            CheckResult("Cat", "Good", "PASS", "fine"),
            CheckResult("Cat", "Bad", "FAIL", "broken", fix="do x"),
        ]
        text = format_report(results, color=False)
        self.assertIn("[PASS] Good", text)
        self.assertIn("[FAIL] Bad", text)
        self.assertIn("fix: do x", text)
        self.assertIn("1 passed, 0 warnings, 1 failed", text)

    def test_exit_code_reflects_failures(self):
        from src.data_guardian import diagnostics

        with patch.object(diagnostics.Doctor, "run_all",
                          return_value=[CheckResult("c", "n", "FAIL", "s")]):
            self.assertEqual(diagnostics.run_doctor(as_json=True, color=False), 1)
        with patch.object(diagnostics.Doctor, "run_all",
                          return_value=[CheckResult("c", "n", "WARN", "s")]):
            self.assertEqual(diagnostics.run_doctor(as_json=True, color=False), 2)
        with patch.object(diagnostics.Doctor, "run_all",
                          return_value=[CheckResult("c", "n", "PASS", "s")]):
            self.assertEqual(diagnostics.run_doctor(as_json=True, color=False), 0)


class TestCliWiring(unittest.TestCase):
    def test_help_lists_commands(self):
        proc = subprocess.run(
            [sys.executable, "-m", "src.data_guardian.main", "--help"],
            cwd=ROOT, text=True, capture_output=True, check=False,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for command in ("status", "daemon", "tray", "dashboard", "doctor",
                        "profile", "allow", "allow-apt", "bypass", "set-hotspot"):
            self.assertIn(command, proc.stdout)

    def test_version_flag(self):
        proc = subprocess.run(
            [sys.executable, "-m", "src.data_guardian.main", "--version"],
            cwd=ROOT, text=True, capture_output=True, check=False,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("data-guardian", proc.stdout)


class TestInstallerSafety(unittest.TestCase):
    def test_installer_never_writes_an_apt_hook(self):
        """BUG-03: the root Python APT hook must not come back."""
        text = (ROOT / "scripts" / "install.sh").read_text()
        self.assertNotIn("/etc/apt/apt.conf.d", text)
        self.assertNotIn("Pre-Invoke", text)
        self.assertNotIn("approvals.gate", text)

    def test_installer_installs_helper_and_sudoers(self):
        text = (ROOT / "scripts" / "install.sh").read_text()
        self.assertIn("data-guardian-helper", text)
        self.assertIn("sudoers", text)
        self.assertIn("visudo -cf", text)
        self.assertIn("gtk-update-icon-cache", text)

    def test_installer_handles_sudo_user(self):
        text = (ROOT / "scripts" / "install.sh").read_text()
        self.assertIn("SUDO_USER", text)
        self.assertIn("getent passwd", text)

    def test_uninstaller_sets_pythonpath(self):
        """BUG-18: remove-shims must actually run."""
        text = (ROOT / "scripts" / "uninstall.sh").read_text()
        self.assertIn("PYTHONPATH", text)
        self.assertIn("remove-shims", text)

    def test_no_root_python_import_anywhere_in_scripts(self):
        """Project code must never be imported by a root-owned interpreter.

        ``sudo -u "$TARGET_USER" ... python3 -m src.data_guardian`` is fine (it
        drops privileges); ``sudo python3 -m src.data_guardian`` is not.
        """
        import re

        root_project_python = re.compile(
            r"^\s*sudo\s+(?!-u\b)(?!.*\b-u\b).*\bpython3?\b.*\bsrc\.data_guardian\b"
        )
        for script in (ROOT / "scripts").glob("*.sh"):
            for lineno, line in enumerate(script.read_text().splitlines(), 1):
                self.assertIsNone(
                    root_project_python.search(line),
                    f"{script.name}:{lineno} runs project code as root: {line.strip()}",
                )

    def test_emergency_script_never_unlinks_socket(self):
        """BUG-31: the live daemon socket must survive a bypass."""
        text = (ROOT / "scripts" / "emergency-disable.sh").read_text()
        self.assertNotIn("rm -f", text)
        self.assertIn("bypass", text)


class TestDeployScript(unittest.TestCase):
    def test_deploy_refuses_git_checkout(self):
        text = (ROOT / "scripts" / "deploy.sh").read_text()
        self.assertIn(".git", text)
        self.assertIn("rsync", text)

    def test_deploy_copies_only_runtime_trees(self):
        text = (ROOT / "scripts" / "deploy.sh").read_text()
        for included in ("/src/", "/scripts/", "/config/", "/systemd/", "/assets/", "/File.md"):
            self.assertIn(included, text)


if __name__ == "__main__":
    unittest.main()
