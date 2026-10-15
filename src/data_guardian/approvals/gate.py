"""Universal approval gate for package managers and download operations."""

from __future__ import annotations
import os
import sys
import argparse
import subprocess
from pathlib import Path
from typing import List, Tuple

from src.data_guardian.config import ConfigManager
from src.data_guardian.database import Database
from src.data_guardian.network.nm_monitor import NetworkMonitor

DOWNLOAD_ACTIONS = {
    "apt": ["install", "upgrade", "dist-upgrade", "full-upgrade", "update", "source"],
    "pip": ["install", "download", "wheel"],
    "npm": ["install", "i", "update", "up", "add"],
    "cargo": ["install", "update", "add"],
}


class ApprovalGate:
    """Evaluates whether a package operation requires user confirmation and prompts the user."""

    def __init__(self, config_manager: ConfigManager | None = None) -> None:
        self.config_mgr = config_manager or ConfigManager()
        self.db = Database(self.config_mgr.db_path)
        self.monitor = NetworkMonitor(
            designated_uuid=self.config_mgr.designated_hotspot_uuid,
            designated_name=self.config_mgr.config.get("hotspot", {}).get("designated_name", "")
        )

    def is_download_action(self, tool: str, args: List[str]) -> bool:
        """Determines if the given arguments represent a network download/install operation."""
        tool_lower = tool.lower()
        if tool_lower not in DOWNLOAD_ACTIONS:
            return True  # Unknown tool -> default to gating if configured

        relevant_actions = DOWNLOAD_ACTIONS[tool_lower]
        for arg in args:
            if not arg.startswith("-"):
                return arg.lower() in relevant_actions
        return False

    def should_gate(self, tool: str, args: List[str]) -> Tuple[bool, str]:
        """Checks policies to determine if the operation must be gated.

        Returns (should_gate: bool, reason: str).
        """
        # 1. Global enable check
        if not self.config_mgr.config.get("app", {}).get("enabled", True):
            return False, "Data Guardian is globally disabled."

        # 2. Profile check
        profile = self.config_mgr.active_profile
        if profile == "off":
            return False, "Profile is set to 'off' (monitoring only)."

        # 3. Hotspot check
        is_hotspot = self.monitor.is_designated_hotspot_active()
        if not is_hotspot:
            # Check if active connection is marked as metered in NetworkManager
            active_conn = self.monitor.get_active_connection()
            if not (active_conn and active_conn.is_metered):
                return False, "Not connected to a metered hotspot."

        # 4. Tool-specific approval setting
        approvals_cfg = self.config_mgr.config.get("approvals", {})
        if not approvals_cfg.get(tool.lower(), True):
            return False, f"Approval for tool '{tool}' is disabled in configuration."

        # 5. Check if arguments involve downloading
        if not self.is_download_action(tool, args):
            return False, f"Action '{' '.join(args)}' does not initiate downloads."

        return True, "Metered connection active; download approval required."

    def prompt_user(self, tool: str, args: List[str]) -> bool:
        """Displays approval prompt via Zenity or interactive terminal.

        Returns True if approved, False if denied/timed out.
        """
        packages_summary = " ".join(args) or "unspecified"
        display = os.environ.get("DISPLAY")
        timeout = int(self.config_mgr.config.get("approvals", {}).get("timeout_seconds", 60))

        dialog_text = (
            f"<b>Linux Data Guardian — Download Approval</b>\n\n"
            f"An application is requesting internet downloads on your <b>metered mobile data</b>.\n\n"
            f"<b>Tool:</b> {tool}\n"
            f"<b>Command / Target:</b> {packages_summary}\n\n"
            f"Do you want to approve this download?"
        )

        approved = False

        # Attempt Zenity GUI prompt if DISPLAY is present
        if display and Path("/usr/bin/zenity").exists():
            try:
                cmd = [
                    "/usr/bin/zenity",
                    "--question",
                    "--title=Linux Data Guardian — Download Approval",
                    f"--text={dialog_text}",
                    f"--timeout={timeout}",
                    "--ok-label=Approve Download",
                    "--cancel-label=Deny / Cancel",
                    "--width=450",
                ]
                res = subprocess.run(cmd, timeout=timeout + 5)
                approved = (res.returncode == 0)
            except Exception as e:
                print(f"[!] Warning: Zenity dialog failed: {e}")
                approved = False
        else:
            # Fallback to terminal prompt if stdin is interactive
            if sys.stdin.isatty():
                print(f"\n[!] Linux Data Guardian: Metered Hotspot Active")
                print(f"Tool '{tool}' wants to download: {packages_summary}")
                try:
                    choice = input("Approve download? (y/N): ").strip().lower()
                    approved = (choice in ("y", "yes"))
                except (EOFError, KeyboardInterrupt):
                    approved = False
            else:
                # Non-interactive without GUI -> fail closed
                approved = False

        # Record decision in database
        decision_str = "approved" if approved else "denied"
        self.db.record_approval_event(
            tool=tool,
            packages=packages_summary,
            estimated_bytes=0,
            decision=decision_str
        )

        # Notify desktop if denied
        if not approved and display and Path("/usr/bin/notify-send").exists():
            subprocess.run([
                "/usr/bin/notify-send",
                "-u", "normal",
                "Linux Data Guardian",
                f"Download blocked for '{tool} {packages_summary}' to save mobile data."
            ], check=False)

        return approved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Data Guardian Universal Approval Gate",
        add_help=False
    )
    parser.add_argument("--tool", required=True, help="Tool name (e.g. apt, pip, npm, cargo)")

    parsed, remaining_args = parser.parse_known_args()
    gate = ApprovalGate()

    should_gate, reason = gate.should_gate(parsed.tool, remaining_args)
    if not should_gate:
        # Pass through without prompt
        sys.exit(0)

    # Prompt user
    approved = gate.prompt_user(parsed.tool, remaining_args)
    if approved:
        print(f"[+] Data Guardian: Operation approved for {parsed.tool}.")
        sys.exit(0)
    else:
        print(f"[-] Data Guardian: Operation DENIED for {parsed.tool}. Cancelled to preserve data.")
        sys.exit(1)


if __name__ == "__main__":
    main()
