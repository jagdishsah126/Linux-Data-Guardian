"""Universal approval gate for package managers and download operations.

The gate is invoked by the transparent CLI shims (``pip``, ``pip3``, ``npm``,
``cargo``, ...) and decides whether a command may reach the network while the
metered hotspot is active.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.data_guardian.config import ConfigManager
from src.data_guardian.database import Database
from src.data_guardian.network.nm_monitor import NetworkMonitor

# Sub-commands that fetch bytes from the network.
DOWNLOAD_ACTIONS: Dict[str, set] = {
    "apt": {"install", "upgrade", "dist-upgrade", "full-upgrade", "update", "source", "build-dep"},
    "apt-get": {"install", "upgrade", "dist-upgrade", "full-upgrade", "update", "source", "build-dep"},
    "aptitude": {"install", "upgrade", "update", "safe-upgrade"},
    "pip": {"install", "download", "wheel"},
    "pip3": {"install", "download", "wheel"},
    "python": set(),
    "python3": set(),
    "npm": {"install", "i", "update", "up", "add", "ci", "create", "dlx", "exec", "x"},
    "npx": {"install", "add", "create", "dlx", "exec", "x"},
    "yarn": {"install", "add", "up", "upgrade", "dlx", "create"},
    "pnpm": {"install", "i", "add", "update", "up", "dlx", "create", "fetch"},
    "cargo": {"install", "update", "add", "fetch", "vendor", "publish"},
    "gem": {"install", "update", "fetch"},
    "go": {"get", "install", "mod"},
    "flatpak": {"install", "update", "repair"},
    "snap": {"install", "refresh"},
}

# Sub-commands that never touch the network.
READ_ONLY_ACTIONS = {
    "list", "show", "info", "view", "search", "freeze", "check", "config", "help",
    "version", "why", "outdated", "--version", "-v", "-vv", "--help", "-h",
    "hash", "ls", "which", "doctor", "env", "get", "cache", "tree", "audit",
}

# Global flags that consume the following token, so it must not be read as a command.
FLAGS_WITH_VALUE = {
    "-c", "--config", "-r", "--requirement", "-t", "--target",
    "-i", "--index-url", "--extra-index-url", "--proxy", "--cert", "--cache-dir",
    "-o", "--output", "--prefix", "--root", "--user", "--python", "-p",
    "--registry", "--tag", "--git", "--path", "--branch", "--features",
}


def _tokens(args: List[str]) -> List[str]:
    return [a for a in args if a]


def find_subcommand(tool: str, args: List[str]) -> Optional[str]:
    """Return the first meaningful sub-command token.

    Handles ``python -m pip install x`` and skips flags together with their values.
    """
    tokens = _tokens(args)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in ("-m", "--module") and i + 1 < len(tokens):
            module = tokens[i + 1]
            if module.startswith("pip"):
                tool = module
            i += 2
            continue
        if token.startswith("-"):
            if token in FLAGS_WITH_VALUE:
                i += 2
                continue
            i += 1
            continue
        return token.lower()
    return None


class ApprovalGate:
    """Evaluates whether a package operation requires confirmation and prompts the user."""

    def __init__(self, config_manager: ConfigManager | None = None) -> None:
        self.config_mgr = config_manager or ConfigManager()
        self.db = Database(
            self.config_mgr.db_path,
            designated_uuid=self.config_mgr.designated_hotspot_uuid,
        )
        self.monitor = NetworkMonitor(
            designated_uuid=self.config_mgr.designated_hotspot_uuid,
            designated_name=self.config_mgr.config.get("hotspot", {}).get("designated_name", ""),
        )

    # ------------------------------------------------------------------ policy

    def is_download_action(self, tool: str, args: List[str]) -> bool:
        """True only when the command would actually fetch data from the network."""
        tool_lower = tool.lower()
        if tool_lower in ("python", "python3"):
            # `python -m pip install x` — inspect the module token.
            tokens = _tokens(args)
            for i, token in enumerate(tokens):
                if token in ("-m", "--module") and i + 1 < len(tokens) and tokens[i + 1].startswith("pip"):
                    tool_lower = tokens[i + 1]
                    break
            else:
                return False

        actions = DOWNLOAD_ACTIONS.get(tool_lower)
        if actions is None:
            return False

        sub = find_subcommand(tool_lower, args)
        if sub is None:
            return False
        if sub in READ_ONLY_ACTIONS:
            return False
        return sub in actions

    def should_gate(self, tool: str, args: List[str]) -> Tuple[bool, str]:
        if not self.config_mgr.app_enabled:
            return False, "Data Guardian is globally disabled."

        active = self.monitor.get_active_connection()
        is_hotspot = self.monitor.matches_designated(active)
        if not is_hotspot and not (active and active.is_metered):
            return False, "Not connected to a metered hotspot."

        # The per-network profile is authoritative, exactly as in the daemon.
        profile = (
            self.config_mgr.get_network_profile(active.uuid)
            if active else self.config_mgr.active_profile
        )
        if profile == "off":
            return False, "Profile is set to 'off' (monitoring only)."

        approvals_cfg = self.config_mgr.config.get("approvals", {})
        if not approvals_cfg.get(tool.lower(), True):
            return False, f"Approval for tool '{tool}' is disabled in configuration."

        if not self.is_download_action(tool, args):
            return False, "Command does not initiate a network download."

        return True, "Metered connection active; download approval required."

    # ------------------------------------------------------------------ prompt

    def _record(self, tool: str, args: List[str], decision: str) -> None:
        try:
            self.db.record_approval_event(
                tool=tool,
                packages=" ".join(args)[:500],
                estimated_bytes=0,
                decision=decision,
            )
        except Exception as exc:  # noqa: BLE001 - auditing must never block a decision
            print(f"[!] Could not record approval event: {exc}", file=sys.stderr)

    def prompt_user(self, tool: str, args: List[str]) -> bool:
        """Ask the user. Returns True when the download may proceed."""
        approvals_cfg = self.config_mgr.config.get("approvals", {})
        timeout = int(approvals_cfg.get("timeout_seconds", 60))
        on_timeout = str(approvals_cfg.get("timeout_action", "deny"))
        display = os.environ.get("DISPLAY")

        try:
            command_str = " ".join(shlex.quote(a) for a in args)
        except Exception:  # noqa: BLE001
            command_str = " ".join(args)
        summary = f"{tool} {command_str}".strip()[:400]

        approved = False
        if display and Path("/usr/bin/zenity").exists():
            try:
                res = subprocess_run([
                    "/usr/bin/zenity", "--question",
                    "--title=Linux Data Guardian — Download Approval",
                    f"--text=<b>Download approval needed</b>\n\nMetered mobile data is active.\n\n"
                    f"<b>Command:</b> {summary}\n\nApprove this download?",
                    f"--timeout={timeout}",
                    "--ok-label=Approve", "--cancel-label=Deny", "--width=460",
                ], timeout=timeout + 5)
                approved = res == 0
            except Exception as exc:  # noqa: BLE001
                print(f"[!] Warning: Zenity dialog failed: {exc}", file=sys.stderr)
                approved = False
        elif sys.stdin.isatty():
            print("\n[!] Linux Data Guardian: metered hotspot active")
            print(f"    Tool '{tool}' wants to download: {command_str}")
            try:
                approved = input("Approve download? (y/N): ").strip().lower() in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                approved = False
        else:
            approved = (on_timeout == "allow")
            print(
                f"[-] No interactive session; applying timeout_action={on_timeout!r} for {summary}",
                file=sys.stderr,
            )

        self._record(tool, args, "approved" if approved else "denied")

        if not approved and display and Path("/usr/bin/notify-send").exists():
            subprocess_run([
                "/usr/bin/notify-send", "-u", "normal", "Linux Data Guardian",
                f"Download blocked: {summary}",
            ], timeout=5)
        return approved


def subprocess_run(cmd: List[str], timeout: float) -> int:
    import subprocess

    return subprocess.run(cmd, timeout=timeout, check=False).returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Data Guardian approval gate", add_help=False)
    parser.add_argument("--tool", required=True)
    parsed, remaining = parser.parse_known_args()

    gate = ApprovalGate()
    should_gate, reason = gate.should_gate(parsed.tool, remaining)
    if not should_gate:
        sys.exit(0)

    if gate.prompt_user(parsed.tool, remaining):
        print(f"[+] Data Guardian: operation approved for {parsed.tool}.")
        sys.exit(0)
    print(f"[-] Data Guardian: operation DENIED for {parsed.tool}.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
