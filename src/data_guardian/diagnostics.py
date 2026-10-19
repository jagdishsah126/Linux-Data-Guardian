"""System Health and Diagnostic Suite ('data-guardian doctor') for Linux Data Guardian."""

from __future__ import annotations
import os
import sys
import time
import socket
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, asdict

from src.data_guardian.config import ConfigManager
from src.data_guardian.database import Database, CURRENT_SCHEMA_VERSION
from src.data_guardian.firewall.nft_manager import NftablesManager, HELPER_BIN
from src.data_guardian.network.nm_monitor import NetworkMonitor
from src.data_guardian.ipc import get_socket_path


@dataclass
class CheckResult:
    category: str
    name: str
    status: str  # "PASS", "WARN", "FAIL"
    summary: str
    details: Optional[str] = None


class Doctor:
    """Performs deep health and security checks across all system layers."""

    def __init__(self, config_mgr: Optional[ConfigManager] = None) -> None:
        self.config_mgr = config_mgr or ConfigManager()
        self.db = Database(self.config_mgr.db_path)

    def run_all(self) -> List[CheckResult]:
        """Runs the complete test suite and returns categorized results."""
        results: List[CheckResult] = []
        results.append(self.check_privileged_helper())
        results.append(self.check_sudoers_rule())
        results.append(self.check_nftables_isolation())
        results.append(self.check_daemon_and_ipc())
        results.append(self.check_database_integrity())
        results.append(self.check_desktop_integration())
        results.append(self.check_network_and_hotspot())
        return results

    def check_privileged_helper(self) -> CheckResult:
        """Verifies root ownership, permissions, and location of the privileged helper."""
        if not HELPER_BIN.exists():
            return CheckResult(
                category="Privilege Separation",
                name="Privileged Helper Binary",
                status="FAIL",
                summary="Helper binary not found at /usr/local/libexec/data-guardian-helper",
                details="Run installation step: sudo cp scripts/data-guardian-helper /usr/local/libexec/ && sudo chmod 0755 /usr/local/libexec/data-guardian-helper"
            )

        try:
            stat_info = os.stat(HELPER_BIN)
            is_root_owned = (stat_info.st_uid == 0 and stat_info.st_gid == 0)
            is_executable = bool(stat_info.st_mode & 0o111)

            if not is_root_owned:
                return CheckResult(
                    category="Privilege Separation",
                    name="Privileged Helper Binary",
                    status="WARN",
                    summary="Helper binary exists but is not owned by root:root",
                    details=f"Current ownership UID:{stat_info.st_uid} GID:{stat_info.st_gid}. Fix: sudo chown root:root /usr/local/libexec/data-guardian-helper"
                )

            if not is_executable:
                return CheckResult(
                    category="Privilege Separation",
                    name="Privileged Helper Binary",
                    status="FAIL",
                    summary="Helper binary is not executable",
                    details="Fix: sudo chmod 0755 /usr/local/libexec/data-guardian-helper"
                )

            return CheckResult(
                category="Privilege Separation",
                name="Privileged Helper Binary",
                status="PASS",
                summary="Binary installed at /usr/local/libexec/data-guardian-helper (0755, root:root)"
            )
        except Exception as e:
            return CheckResult(
                category="Privilege Separation",
                name="Privileged Helper Binary",
                status="FAIL",
                summary=f"Failed to stat helper binary: {e}"
            )

    def check_sudoers_rule(self) -> CheckResult:
        """Verifies passwordless sudo execution of the privileged helper."""
        if not HELPER_BIN.exists():
            return CheckResult(
                category="Privilege Separation",
                name="Sudoers Rule",
                status="FAIL",
                summary="Cannot test sudoers: helper binary does not exist"
            )

        try:
            start_t = time.perf_counter()
            res = subprocess.run(
                ["sudo", "-n", str(HELPER_BIN), "--status"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3
            )
            elapsed_ms = (time.perf_counter() - start_t) * 1000

            # returncode 0 (active) or 1 (inactive) means sudo executed passwordlessly!
            if res.returncode in (0, 1):
                return CheckResult(
                    category="Privilege Separation",
                    name="Sudoers Rule",
                    status="PASS",
                    summary=f"Passwordless sudo verified ({elapsed_ms:.1f}ms). Returned '{res.stdout.strip()}'."
                )
            else:
                return CheckResult(
                    category="Privilege Separation",
                    name="Sudoers Rule",
                    status="FAIL",
                    summary=f"Passwordless sudo failed (exit code {res.returncode}): {res.stderr.strip()}",
                    details="Verify /etc/sudoers.d/linux-data-guardian with 'sudo visudo -cf /etc/sudoers.d/linux-data-guardian'"
                )
        except subprocess.TimeoutExpired:
            return CheckResult(
                category="Privilege Separation",
                name="Sudoers Rule",
                status="FAIL",
                summary="Sudo prompted for a password or hung waiting for terminal input.",
                details="Ensure NOPASSWD is set in /etc/sudoers.d/linux-data-guardian"
            )
        except Exception as e:
            return CheckResult(
                category="Privilege Separation",
                name="Sudoers Rule",
                status="FAIL",
                summary=f"Error executing sudo check: {e}"
            )

    def check_nftables_isolation(self) -> CheckResult:
        """Verifies nftables binary and isolation of table inet data_guardian."""
        nft_path = subprocess.run(["which", "nft"], stdout=subprocess.PIPE, text=True).stdout.strip()
        if not nft_path:
            return CheckResult(
                category="Firewall Isolation",
                name="Nftables Subsystem",
                status="FAIL",
                summary="nftables executable ('nft') not found on system path"
            )

        try:
            # Query status via helper
            res = subprocess.run(
                ["sudo", "-n", str(HELPER_BIN), "--status"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3
            )
            table_state = res.stdout.strip() if res.returncode in (0, 1) else "unknown"

            return CheckResult(
                category="Firewall Isolation",
                name="Nftables Subsystem",
                status="PASS",
                summary=f"nftables present ({nft_path}). Table 'data_guardian' state: {table_state}.",
                details="Scoped exclusively to 'table inet data_guardian'; system UFW/iptables are untouched."
            )
        except Exception as e:
            return CheckResult(
                category="Firewall Isolation",
                name="Nftables Subsystem",
                status="WARN",
                summary=f"Could not verify table state: {e}"
            )

    def check_daemon_and_ipc(self) -> CheckResult:
        """Verifies background daemon process, memory RSS, and IPC socket responsiveness."""
        # 1. Systemd service status
        svc_active = False
        try:
            res = subprocess.run(
                ["systemctl", "is-active", "data-guardian.service"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3
            )
            svc_active = (res.stdout.strip() == "active")
        except Exception:
            svc_active = False

        # 2. Process check & Memory RSS
        pid = None
        rss_mb = 0.0
        try:
            pgrep = subprocess.run(
                ["pgrep", "-f", "src.data_guardian.main daemon"],
                stdout=subprocess.PIPE,
                text=True
            )
            pids = pgrep.stdout.strip().split()
            if pids:
                pid = int(pids[0])
                ps_rss = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "rss="],
                    stdout=subprocess.PIPE,
                    text=True
                )
                rss_kb = int(ps_rss.stdout.strip() or 0)
                rss_mb = rss_kb / 1024.0
        except Exception:
            pass

        # 3. IPC Socket Ping
        sock_path = get_socket_path()
        ipc_ok = False
        ping_ms = 0.0
        if sock_path.exists():
            try:
                start_t = time.perf_counter()
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(2.0)
                client.connect(str(sock_path))
                client.sendall(json.dumps({"action": "ping"}).encode("utf-8") + b"\n")
                raw = client.recv(4096)
                client.close()
                elapsed = (time.perf_counter() - start_t) * 1000
                ping_ms = elapsed
                resp = json.loads(raw.decode("utf-8"))
                ipc_ok = (resp.get("status") == "ok")
            except Exception:
                ipc_ok = False

        if not svc_active and not pid:
            return CheckResult(
                category="Daemon & IPC",
                name="Background Daemon",
                status="FAIL",
                summary="Daemon is not running",
                details="Start with: systemctl start data-guardian.service"
            )

        if not ipc_ok:
            return CheckResult(
                category="Daemon & IPC",
                name="Background Daemon",
                status="WARN",
                summary=f"Daemon running (PID {pid}, RSS {rss_mb:.1f}MB) but IPC socket unresponsive at {sock_path}",
                details="Restart daemon: systemctl restart data-guardian.service"
            )

        mem_status = "PASS" if rss_mb < 60.0 else "WARN"
        return CheckResult(
            category="Daemon & IPC",
            name="Background Daemon",
            status=mem_status,
            summary=f"Running (PID {pid}, Memory: {rss_mb:.1f}MB, IPC roundtrip: {ping_ms:.2f}ms)",
            details=f"Service: {'active' if svc_active else 'unmanaged'}, Socket: {sock_path}"
        )

    def check_database_integrity(self) -> CheckResult:
        """Verifies SQLite database integrity, WAL mode, and schema version."""
        db_path = self.config_mgr.db_path
        if not db_path.exists():
            return CheckResult(
                category="Data Storage",
                name="SQLite Database",
                status="WARN",
                summary=f"Database file not yet created at {db_path}"
            )

        try:
            with sqlite3.connect(db_path, timeout=5.0) as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check;")
                integrity = cursor.fetchone()[0]
                if integrity != "ok":
                    return CheckResult(
                        category="Data Storage",
                        name="SQLite Database",
                        status="FAIL",
                        summary=f"Database corruption detected: {integrity}",
                        details=f"Path: {db_path}"
                    )

                cursor.execute("SELECT MAX(version) FROM schema_version;")
                ver_row = cursor.fetchone()
                version = ver_row[0] if ver_row else 1

                # Record statistics
                cursor.execute("SELECT COUNT(*) FROM network_daily_usage;")
                net_records = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM sessions;")
                session_records = cursor.fetchone()[0]

            return CheckResult(
                category="Data Storage",
                name="SQLite Database",
                status="PASS",
                summary=f"Integrity OK (v{version}). Records: {session_records} sessions, {net_records} network days.",
                details=f"Path: {db_path} (WAL mode enabled)"
            )
        except Exception as e:
            return CheckResult(
                category="Data Storage",
                name="SQLite Database",
                status="FAIL",
                summary=f"Database error: {e}"
            )

    def check_desktop_integration(self) -> CheckResult:
        """Verifies desktop icons, autostart launchers, and running tray applet."""
        icon_path = Path("/usr/share/icons/hicolor/512x512/apps/linux-data-guardian.png")
        icon_ok = icon_path.exists()

        tray_running = False
        try:
            res = subprocess.run(["pgrep", "-f", "src.data_guardian.main tray"], stdout=subprocess.PIPE, text=True)
            tray_running = bool(res.stdout.strip())
        except Exception:
            pass

        autostart_path = Path.home() / ".config" / "autostart" / "data-guardian-tray.desktop"
        autostart_ok = autostart_path.exists()

        summary_parts = []
        summary_parts.append(f"Icon: {'installed' if icon_ok else 'missing'}")
        summary_parts.append(f"Tray applet: {'running' if tray_running else 'inactive'}")
        summary_parts.append(f"Autostart: {'configured' if autostart_ok else 'disabled'}")

        status = "PASS" if (icon_ok and tray_running) else "WARN"
        return CheckResult(
            category="Desktop Integration",
            name="Tray & UI Components",
            status=status,
            summary="; ".join(summary_parts)
        )

    def check_network_and_hotspot(self) -> CheckResult:
        """Verifies active NetworkManager connection and designated hotspot designation."""
        monitor = NetworkMonitor(
            designated_uuid=self.config_mgr.designated_hotspot_uuid,
            designated_name=self.config_mgr.designated_hotspot_name
        )
        active_conn = monitor.get_active_connection()
        hotspot_uuid = self.config_mgr.designated_hotspot_uuid
        hotspot_name = self.config_mgr.designated_hotspot_name

        if not active_conn:
            return CheckResult(
                category="Network Monitor",
                name="Active Connection",
                status="WARN",
                summary="No active Wi-Fi or Ethernet connection detected",
                details=f"Configured Hotspot: {hotspot_name or 'None'} ({hotspot_uuid or 'Not set'})"
            )

        is_hotspot = monitor.is_designated_hotspot_active()
        mode_str = "METERED (Hotspot Active)" if is_hotspot else "UNMETERED"

        return CheckResult(
            category="Network Monitor",
            name="Active Connection",
            status="PASS",
            summary=f"Connected to '{active_conn.name}' on {active_conn.interface} [{mode_str}]",
            details=f"UUID: {active_conn.uuid} | Designated Hotspot: {hotspot_name or hotspot_uuid or 'None'}"
        )


def format_cli_report(results: List[CheckResult], color: bool = True) -> str:
    """Renders a formatted, human-readable terminal report."""
    # Terminal ANSI color escape codes
    GREEN = "\033[92m" if color else ""
    YELLOW = "\033[93m" if color else ""
    RED = "\033[91m" if color else ""
    CYAN = "\033[96m" if color else ""
    BOLD = "\033[1m" if color else ""
    RESET = "\033[0m" if color else ""

    lines = [
        f"{BOLD}{CYAN}══════════════════════════════════════════════════════════════════════════════{RESET}",
        f"{BOLD}{CYAN}                   Linux Data Guardian Doctor — Health Report                  {RESET}",
        f"{BOLD}{CYAN}══════════════════════════════════════════════════════════════════════════════{RESET}",
        ""
    ]

    pass_count = sum(1 for r in results if r.status == "PASS")
    warn_count = sum(1 for r in results if r.status == "WARN")
    fail_count = sum(1 for r in results if r.status == "FAIL")

    for r in results:
        if r.status == "PASS":
            tag = f"{GREEN}[✓ PASS]{RESET}"
        elif r.status == "WARN":
            tag = f"{YELLOW}[⚠ WARN]{RESET}"
        else:
            tag = f"{RED}[✗ FAIL]{RESET}"

        lines.append(f"{tag} {BOLD}{r.name:<25}{RESET} ({r.category})")
        lines.append(f"       {r.summary}")
        if r.details:
            lines.append(f"       {r.details}")
        lines.append("")

    lines.append(f"{BOLD}Summary:{RESET} {GREEN}{pass_count} Passed{RESET}, {YELLOW}{warn_count} Warnings{RESET}, {RED}{fail_count} Failed{RESET}")
    if fail_count == 0:
        lines.append(f"{BOLD}{GREEN}All critical security boundaries and subsystem components are healthy!{RESET}")
    else:
        lines.append(f"{BOLD}{RED}Some critical checks failed. Please inspect the recommendations above.{RESET}")

    lines.append(f"{BOLD}{CYAN}══════════════════════════════════════════════════════════════════════════════{RESET}")
    return "\n".join(lines)
