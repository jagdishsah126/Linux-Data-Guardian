"""Applies and removes isolated nftables rulesets for Linux Data Guardian."""

from __future__ import annotations
import subprocess
from typing import Optional, Tuple
from pathlib import Path

from src.data_guardian.firewall.rulesets import TABLE_NAME, build_nft_ruleset


class NftablesManager:
    """Safely manages the isolated 'data_guardian' nftables table."""

    def __init__(self) -> None:
        self.active_profile: str = "off"
        self.is_quota_exceeded: bool = False

    @staticmethod
    def is_nft_installed() -> bool:
        """Check if nft binary is available."""
        return Path("/usr/sbin/nft").exists() or Path("/usr/bin/nft").exists()

    @staticmethod
    def can_apply_rules() -> bool:
        """Checks if current process has root or passwordless sudo to apply nft rules."""
        import os
        if os.geteuid() == 0:
            return True
        try:
            res = subprocess.run(["sudo", "-n", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return res.returncode == 0
        except Exception:
            return False

    def apply_profile(self, profile: str, quota_exceeded: bool = False) -> Tuple[bool, str]:
        """Generates and atomically commits rules for the specified profile."""
        if not self.is_nft_installed():
            return False, "nftables binary not found."

        if profile == "off" and not quota_exceeded:
            return self.flush()

        ruleset = build_nft_ruleset(profile=profile, quota_exceeded=quota_exceeded)

        # Check privileges
        use_sudo = []
        import os
        if os.geteuid() != 0:
            if self.can_apply_rules():
                use_sudo = ["sudo", "-n"]
            else:
                return False, "Root privileges required to apply firewall rules."

        cmd = use_sudo + ["nft", "-f", "-"]
        try:
            res = subprocess.run(cmd, input=ruleset, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
            if res.returncode == 0:
                self.active_profile = profile
                self.is_quota_exceeded = quota_exceeded
                return True, f"Successfully applied profile '{profile}' (quota_exceeded={quota_exceeded})."
            else:
                return False, f"nft command failed: {res.stderr.strip()}"
        except Exception as e:
            return False, f"Exception applying nft rules: {e}"

    def flush(self) -> Tuple[bool, str]:
        """Completely removes the 'data_guardian' nftables table, restoring unrestricted networking."""
        if not self.is_nft_installed():
            return True, "nftables not installed."

        use_sudo = []
        import os
        if os.geteuid() != 0:
            if self.can_apply_rules():
                use_sudo = ["sudo", "-n"]
            else:
                return False, "Root privileges required to flush firewall rules."

        cmd = use_sudo + ["nft", "delete", "table", "inet", TABLE_NAME]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
            self.active_profile = "off"
            self.is_quota_exceeded = False
            return True, "Successfully flushed data_guardian table."
        except Exception as e:
            return False, f"Exception flushing nft table: {e}"
