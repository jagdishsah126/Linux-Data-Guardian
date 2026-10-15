"""Unit tests for nftables ruleset generation."""

import unittest
from src.data_guardian.firewall.rulesets import build_nft_ruleset, TABLE_NAME


class TestFirewallRules(unittest.TestCase):
    def test_balanced_ruleset(self):
        rules = build_nft_ruleset(profile="balanced", quota_exceeded=False)
        self.assertIn(f"table inet {TABLE_NAME}", rules)
        self.assertIn("oif \"lo\" accept", rules)
        self.assertIn("ct state established,related accept", rules)
        self.assertIn("udp dport {53, 67, 68, 123} accept", rules)
        self.assertIn("skuid 42 ct state new drop", rules)

    def test_strict_ruleset(self):
        rules = build_nft_ruleset(profile="strict", quota_exceeded=False)
        self.assertIn(f"table inet {TABLE_NAME}", rules)
        self.assertIn("data_guardian/foreground", rules)
        self.assertIn("ct state new drop", rules)

    def test_quota_exceeded_ruleset(self):
        rules = build_nft_ruleset(profile="balanced", quota_exceeded=True)
        self.assertIn(f"table inet {TABLE_NAME}", rules)
        self.assertIn("ct state new drop", rules)

    def test_off_profile_returns_delete(self):
        rules = build_nft_ruleset(profile="off", quota_exceeded=False)
        self.assertEqual(rules.strip(), f"delete table inet {TABLE_NAME}")


if __name__ == "__main__":
    unittest.main()
