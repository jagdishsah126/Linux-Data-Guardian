"""Applies and removes the isolated nftables table via the privileged helper.

The ruleset text is produced by :mod:`src.data_guardian.firewall.rulesets` and
handed to the helper on stdin, which validates it with ``nft -c`` before
committing. The helper never interprets user-controlled strings.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from src.data_guardian.firewall.rulesets import build_ruleset, is_valid_interface

HELPER_BIN = Path("/usr/local/libexec/data-guardian-helper")
FALLBACK_HELPER_BIN = (
    Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "data-guardian-helper"
)


class NftablesManager:
    """Manages the isolated 'data_guardian' nftables table."""

    def __init__(self) -> None:
        self.active_profile: str = "off"
        self.is_quota_exceeded: bool = False
        self.last_error: Optional[str] = None
        self._cgroups_ready: Optional[bool] = None

    # --- helper plumbing ------------------------------------------------------

    @staticmethod
    def get_helper_path() -> Path:
        if HELPER_BIN.exists() and os.access(HELPER_BIN, os.X_OK):
            return HELPER_BIN
        return FALLBACK_HELPER_BIN

    def _helper_cmd(self, *args: str) -> list[str]:
        helper = str(self.get_helper_path())
        if os.geteuid() == 0:
            return [helper, *args]
        return ["sudo", "-n", helper, *args]

    def _run(
        self, args: list[str], *, stdin: Optional[str] = None, timeout: float = 8.0
    ) -> Tuple[int, str, str]:
        try:
            res = subprocess.run(
                args,
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            return res.returncode, res.stdout.strip(), res.stderr.strip()
        except FileNotFoundError:
            return 127, "", "helper not found"
        except subprocess.TimeoutExpired:
            return 124, "", "helper timed out"
        except Exception as exc:  # noqa: BLE001
            return 1, "", str(exc)

    @classmethod
    def can_apply_rules(cls) -> bool:
        """True when the helper can be invoked without an interactive password.

        Probe the helper itself, not ``sudo -n true``: the sudoers rule grants
        exactly one command, so a generic sudo probe fails even on a perfectly
        healthy installation. A genuine ``--status`` returns 0 (active) or 1
        (inactive); a sudo failure returns 1 with no status on stdout.
        """
        if os.geteuid() == 0:
            return True
        if not cls.get_helper_path().exists():
            return False
        mgr = cls()
        try:
            code, out, _err = mgr._run(mgr._helper_cmd("--status"))
        except Exception:  # noqa: BLE001
            return False
        return code in (0, 1) and out in ("active", "inactive")

    # --- cgroup support -------------------------------------------------------

    def ensure_cgroups(self) -> bool:
        """Create the foreground/whitelist cgroups used by strict mode."""
        if self._cgroups_ready is not None:
            return self._cgroups_ready
        code, _, _ = self._run(self._helper_cmd("--cgroup-init"))
        self._cgroups_ready = (code == 0)
        return self._cgroups_ready

    def cgroup_set(self, group: str, pids: list[int]) -> Tuple[bool, str]:
        if group not in ("foreground", "whitelist"):
            return False, f"unknown cgroup {group!r}"
        if not pids:
            code, out, err = self._run(self._helper_cmd("--cgroup-clear", group))
        else:
            code, out, err = self._run(
                self._helper_cmd("--cgroup-set", group, *[str(p) for p in pids])
            )
        return code == 0, (out or err)

    # --- profile application ---------------------------------------------------

    def apply_profile(
        self,
        profile: str,
        interface: str = "",
        quota_exceeded: bool = False,
        apt_window: bool = False,
        strict_use_cgroups: bool = True,
        extra_tcp_ports: Optional[list[int]] = None,
    ) -> Tuple[bool, str]:
        """Render and atomically apply the ruleset for ``profile``."""
        if profile == "off" and not quota_exceeded and not apt_window:
            return self.flush()

        if not is_valid_interface(interface):
            self.last_error = f"no usable interface (got {interface!r})"
            return False, self.last_error

        if not self.can_apply_rules():
            self.last_error = (
                "Privileged helper not accessible (missing helper or passwordless sudo)."
            )
            return False, self.last_error

        use_cgroups = False
        if profile == "strict" and not quota_exceeded and strict_use_cgroups:
            use_cgroups = self.ensure_cgroups()

        try:
            ruleset = build_ruleset(
                profile=profile,
                interface=interface,
                quota_exceeded=quota_exceeded,
                apt_window=apt_window,
                strict_use_cgroups=use_cgroups,
                extra_tcp_ports=extra_tcp_ports,
            )
        except ValueError as exc:
            self.last_error = str(exc)
            return False, self.last_error

        code, out, err = self._run(self._helper_cmd("--apply-stdin"), stdin=ruleset)
        if code != 0:
            self.last_error = f"apply failed (rc={code}): {err or out}"
            return False, self.last_error

        self.active_profile = profile
        self.is_quota_exceeded = quota_exceeded
        self.last_error = None
        note = "" if not (profile == "strict" and not use_cgroups) else " (port fallback, cgroups unavailable)"
        return True, (out or "applied") + note

    def open_apt_window(self, interface: str = "") -> Tuple[bool, str]:
        """Re-apply the current profile with the APT window opened (the daemon owns the timer)."""
        if not is_valid_interface(interface):
            return False, f"no usable interface (got {interface!r})"
        return self.apply_profile(
            self.active_profile if self.active_profile != "off" else "balanced",
            interface=interface,
            quota_exceeded=self.is_quota_exceeded,
            apt_window=True,
        )

    def close_apt_window(self, interface: str = "") -> Tuple[bool, str]:
        return self.apply_profile(
            self.active_profile if self.active_profile != "off" else "balanced",
            interface=interface,
            quota_exceeded=self.is_quota_exceeded,
            apt_window=False,
        )

    def flush(self) -> Tuple[bool, str]:
        """Remove the 'data_guardian' table. Verifies that it is actually gone."""
        if not self.can_apply_rules():
            self.last_error = "Privileged helper not accessible."
            return False, self.last_error

        code, out, err = self._run(self._helper_cmd("--flush"))
        if code != 0:
            self.last_error = f"flush failed (rc={code}): {err or out}"
            return False, self.last_error

        self.active_profile = "off"
        self.is_quota_exceeded = False
        self.last_error = None
        return True, out or "flushed"

    def table_present(self) -> Optional[bool]:
        """True/False if determinable, None when the helper is unavailable."""
        code, _, _ = self._run(self._helper_cmd("--status"))
        if code in (0, 1):
            return code == 0
        return None
