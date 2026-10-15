"""Applies and removes isolated nftables rulesets for Linux Data Guardian."""

from __future__ import annotations
import os
import subprocess
from typing import Optional, Tuple
from pathlib import Path

HELPER_BIN = Path("/usr/local/libexec/data-guardian-helper")
FALLBACK_HELPER_BIN = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "data-guardian-helper"


class NftablesManager:
    """Safely manages the isolated 'data_guardian' nftables table via the privileged helper."""

    def __init__(self) -> None:
        self.active_profile: str = "off"
        self.is_quota_exceeded: bool = False

    @staticmethod
    def get_helper_path() -> Path:
        """Returns the privileged helper path if available."""
        if HELPER_BIN.exists() and os.access(HELPER_BIN, os.X_OK):
            return HELPER_BIN
        return FALLBACK_HELPER_BIN

    @classmethod
    def can_apply_rules(cls) -> bool:
        """Checks if current process has root or passwordless sudo to invoke the helper."""
        if os.geteuid() == 0:
            return True
        helper = cls.get_helper_path()
        if not helper.exists():
            return False
        try:
            res = subprocess.run(
                ["sudo", "-n", str(helper), "--status"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3
            )
            # Exit code 0 (active) or 1 (inactive) means sudo succeeded without password prompt
            return res.returncode in (0, 1)
        except Exception:
            return False

    def apply_profile(
        self, profile: str, interface: str = "", quota_exceeded: bool = False
    ) -> Tuple[bool, str]:
        """Atomically applies the specified firewall profile on the network interface."""
        if not self.can_apply_rules():
            return False, "Privileged helper not accessible or passwordless sudo missing. Complete sudo install steps."

        if profile == "off" and not quota_exceeded:
            return self.flush()

        helper = str(self.get_helper_path())
        iface = interface or "wlp0s20f3"

        if quota_exceeded:
            action = "--apply-blocked"
        elif profile == "strict":
            action = "--apply-strict"
        elif profile == "balanced":
            action = "--apply-balanced"
        else:
            action = "--apply-balanced"

        cmd = ["sudo", "-n", helper, action, iface] if os.geteuid() != 0 else [helper, action, iface]

        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5
            )
            if res.returncode == 0:
                self.active_profile = profile
                self.is_quota_exceeded = quota_exceeded
                return True, res.stdout.strip()
            else:
                # Safe rollback: flush any broken table
                self.flush()
                return False, f"Helper failed (code {res.returncode}): {res.stderr.strip()}"
        except Exception as e:
            self.flush()
            return False, f"Exception applying firewall rules: {e}"

    def open_apt_window(self, interface: str = "") -> Tuple[bool, str]:
        """Temporarily permits UID 42 (_apt) downloads for user-initiated updates."""
        if not self.can_apply_rules():
            return False, "Privileged helper not accessible."
        helper = str(self.get_helper_path())
        iface = interface or "wlp0s20f3"
        cmd = ["sudo", "-n", helper, "--allow-apt-window", iface] if os.geteuid() != 0 else [helper, "--allow-apt-window", iface]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
            return (res.returncode == 0), (res.stdout.strip() if res.returncode == 0 else res.stderr.strip())
        except Exception as e:
            return False, f"Exception opening APT window: {e}"

    def flush(self) -> Tuple[bool, str]:
        """Completely removes the 'data_guardian' nftables table, restoring unrestricted networking."""
        helper = str(self.get_helper_path())
        cmd = ["sudo", "-n", helper, "--flush"] if os.geteuid() != 0 else [helper, "--flush"]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
            self.active_profile = "off"
            self.is_quota_exceeded = False
            return True, res.stdout.strip()
        except Exception as e:
            return False, f"Exception flushing nft table: {e}"
