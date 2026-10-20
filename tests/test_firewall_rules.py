"""Ruleset rendering + privileged helper safety (regression tests for BUG-04/09/21/22)."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from src.data_guardian.firewall.nft_manager import NftablesManager
from src.data_guardian.firewall.rulesets import (
    TABLE,
    build_ruleset,
    is_valid_interface,
)

HELPER = Path(__file__).resolve().parent.parent / "scripts" / "data-guardian-helper"


class TestRulesetRendering(unittest.TestCase):
    def test_every_profile_starts_with_destroy(self):
        """BUG-04: applying twice must replace, not append."""
        for profile in ("balanced", "strict"):
            rules = build_ruleset(profile=profile, interface="wlan0")
            self.assertTrue(rules.startswith(f"destroy table inet {TABLE}"), profile)

    def test_off_profile_only_destroys(self):
        self.assertEqual(
            build_ruleset(profile="off").strip(),
            f"destroy table inet {TABLE}",
        )

    def test_balanced_blocks_apt_uid(self):
        rules = build_ruleset(profile="balanced", interface="wlan0")
        self.assertIn('oif "wlan0" skuid 42 ct state new drop', rules)
        self.assertNotIn("ct state new drop\n", rules.replace(
            'oif "wlan0" skuid 42 ct state new drop', ""))

    def test_strict_uses_cgroups_by_default(self):
        rules = build_ruleset(profile="strict", interface="wlan0")
        self.assertIn('socket cgroupv2 level 2 "data_guardian/foreground"', rules)
        self.assertIn('socket cgroupv2 level 2 "data_guardian/whitelist"', rules)
        self.assertIn('oif "wlan0" ct state new drop', rules)

    def test_strict_fallback_allowlists_web_ports(self):
        rules = build_ruleset(profile="strict", interface="wlan0", strict_use_cgroups=False)
        self.assertIn('oif "wlan0" tcp dport {80, 443, 22} ct state new accept', rules)
        self.assertNotIn("cgroupv2", rules)

    def test_apt_window_precedes_drop(self):
        """BUG-05: the accept rule must come before the drop rule."""
        rules = build_ruleset(profile="balanced", interface="wlan0", apt_window=True)
        accept = rules.index("skuid 42 ct state new accept")
        drop = rules.index("skuid 42 ct state new drop")
        self.assertLess(accept, drop)

    def test_quota_blocked_drops_established(self):
        rules = build_ruleset(profile="balanced", interface="wlan0", quota_exceeded=True)
        self.assertIn('oif "wlan0" drop', rules)
        self.assertNotIn("ct state established,related accept", rules)
        self.assertIn("udp dport {53, 67, 68} accept", rules)

    def test_invalid_interface_rejected(self):
        for bad in ("", "wlan0; rm -rf /", 'wlan"0', "a" * 16, "wlan0\n"):
            with self.assertRaises(ValueError, msg=bad):
                build_ruleset(profile="balanced", interface=bad)
        self.assertFalse(is_valid_interface("wlan0; rm"))

    def test_invalid_profile_rejected(self):
        with self.assertRaises(ValueError):
            build_ruleset(profile="turbo", interface="wlan0")


class TestRulesetSyntax(unittest.TestCase):
    """Validate with the real nft parser inside an unprivileged user namespace."""

    def _check(self, rules: str) -> None:
        if not shutil.which("nft") or not shutil.which("unshare"):
            self.skipTest("nft/unshare unavailable")
        script = (
            "set -e; "
            "mkdir -p /sys/fs/cgroup/data_guardian/foreground /sys/fs/cgroup/data_guardian/whitelist 2>/dev/null || true; "
            "nft -c -f -"
        )
        proc = subprocess.run(
            ["unshare", "-Urn", "bash", "-c", script],
            input=rules, text=True, capture_output=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_balanced_valid(self):
        self._check(build_ruleset(profile="balanced", interface="lo"))

    def test_blocked_valid(self):
        self._check(build_ruleset(profile="balanced", interface="lo", quota_exceeded=True))

    def test_strict_port_fallback_valid(self):
        self._check(build_ruleset(profile="strict", interface="lo", strict_use_cgroups=False))

    def test_strict_cgroup_rules_valid(self):
        """cgroup matches must parse against a real cgroup path.

        nft rejects ``socket cgroupv2`` unless the path exists, so point the
        renderer at this process's own cgroup (which always exists) instead of
        the production ``data_guardian/...`` paths, which only root creates.
        """
        cgroup_root = Path("/sys/fs/cgroup")
        rel = ""
        try:
            for line in Path("/proc/self/cgroup").read_text().splitlines():
                if line.startswith("0::"):
                    rel = line[3:].strip("/")
                    break
        except OSError:
            self.skipTest("/proc/self/cgroup unavailable")
        if not rel or not (cgroup_root / rel).is_dir():
            self.skipTest("no accessible cgroup2 path")
        rules = build_ruleset(
            profile="strict", interface="lo", strict_use_cgroups=True,
            cgroup_foreground=rel, cgroup_whitelist=rel,
        )
        self.assertIn(f'socket cgroupv2 level {len(rel.split("/"))} "{rel}"', rules)
        self._check(rules)


class TestManagerArguments(unittest.TestCase):
    def test_extra_ports_reach_the_renderer(self):
        mgr = NftablesManager()
        with patch.object(NftablesManager, "can_apply_rules", return_value=True), \
             patch.object(NftablesManager, "ensure_cgroups", return_value=False), \
             patch.object(NftablesManager, "_run", return_value=(0, "applied", "")), \
             patch("src.data_guardian.firewall.nft_manager.build_ruleset",
                   return_value="ruleset") as build:
            ok, _ = mgr.apply_profile(
                "strict", interface="wlan0", extra_tcp_ports=[8080]
            )
        self.assertTrue(ok)
        self.assertEqual(build.call_args.kwargs["extra_tcp_ports"], [8080])
        self.assertFalse(build.call_args.kwargs["strict_use_cgroups"])


class TestHelperProbe(unittest.TestCase):
    """The daemon must probe the helper, not run a generic ``sudo -n true``."""

    @unittest.skipIf(os.geteuid() == 0, "root needs no sudo probe")
    def test_inactive_table_still_counts_as_accessible(self):
        with patch.object(NftablesManager, "get_helper_path",
                          return_value=Path("/usr/local/libexec/data-guardian-helper")), \
             patch.object(NftablesManager, "_run", return_value=(1, "inactive", "")) as run:
            self.assertTrue(NftablesManager.can_apply_rules())
            self.assertIn("--status", run.call_args.args[0])

    @unittest.skipIf(os.geteuid() == 0, "root needs no sudo probe")
    def test_sudo_denial_is_not_accessibility(self):
        with patch.object(NftablesManager, "get_helper_path",
                          return_value=Path("/usr/local/libexec/data-guardian-helper")), \
             patch.object(NftablesManager, "_run",
                          return_value=(1, "", "sudo: a password is required")):
            self.assertFalse(NftablesManager.can_apply_rules())


class TestHelperValidation(unittest.TestCase):
    def setUp(self):
        if not HELPER.exists():
            self.skipTest("helper missing")
        self.helper = str(HELPER)

    def run_helper(self, *args, stdin=None):
        return subprocess.run(
            [self.helper, *args], input=stdin, text=True,
            capture_output=True, check=False,
        )

    def test_rejects_unknown_action(self):
        proc = self.run_helper("--pwn")
        self.assertNotEqual(proc.returncode, 0)
        # Unprivileged callers are refused before parsing (exit 1); root gets
        # the usage error (exit 2).
        if __import__("os").geteuid() == 0:
            self.assertEqual(proc.returncode, 2)

    def test_rejects_foreign_table(self):
        rules = "table inet other { chain output { type filter hook output priority 0; } }\n"
        proc = self.run_helper("--apply-stdin", stdin=rules)
        self.assertNotEqual(proc.returncode, 0)

    def test_rejects_flush_ruleset(self):
        proc = self.run_helper("--apply-stdin", stdin="flush ruleset\ntable inet data_guardian {}\n")
        self.assertNotEqual(proc.returncode, 0)

    def test_rejects_include(self):
        proc = self.run_helper("--apply-stdin", stdin='include "/etc/passwd"\n')
        self.assertNotEqual(proc.returncode, 0)

    def test_rejects_cgroup_traversal(self):
        proc = self.run_helper("--cgroup-set", "../../etc", "1")
        self.assertNotEqual(proc.returncode, 0)

    def test_rejects_bad_pid(self):
        proc = self.run_helper("--cgroup-set", "foreground", "abc")
        self.assertNotEqual(proc.returncode, 0)
        proc = self.run_helper("--cgroup-set", "foreground", "0")
        self.assertNotEqual(proc.returncode, 0)

    def test_non_root_refuses(self):
        if __import__("os").geteuid() == 0:
            self.skipTest("running as root")
        proc = self.run_helper("--status")
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()
