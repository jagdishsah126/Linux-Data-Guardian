"""System health and diagnostic suite (``data-guardian doctor``)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from src.data_guardian.config import ConfigManager
from src.data_guardian.database import CURRENT_SCHEMA_VERSION, Database
from src.data_guardian.firewall.nft_manager import HELPER_BIN, NftablesManager
from src.data_guardian.ipc import get_socket_path, send_ipc_command
from src.data_guardian.network.nm_monitor import NetworkMonitor

SUDOERS_FILE = Path("/etc/sudoers.d/linux-data-guardian")
UNIT_FILE = Path("/etc/systemd/system/data-guardian.service")
ICON_FILE = Path("/usr/share/icons/hicolor/512x512/apps/linux-data-guardian.png")


@dataclass
class CheckResult:
    category: str
    name: str
    status: str  # PASS | WARN | FAIL
    summary: str
    details: Optional[str] = None
    fix: Optional[str] = None


class Doctor:
    """Deep health and security checks across every layer."""

    def __init__(self, config_mgr: Optional[ConfigManager] = None) -> None:
        self.config_mgr = config_mgr or ConfigManager()
        self.db = Database(
            self.config_mgr.db_path,
            designated_uuid=self.config_mgr.designated_hotspot_uuid,
        )

    def run_all(self) -> List[CheckResult]:
        return [
            self.check_python_dependencies(),
            self.check_privileged_helper(),
            self.check_helper_capabilities(),
            self.check_sudoers_rule(),
            self.check_nftables(),
            self.check_strict_cgroups(),
            self.check_daemon_and_ipc(),
            self.check_database_integrity(),
            self.check_file_permissions(),
            self.check_desktop_integration(),
            self.check_network_and_hotspot(),
        ]

    # ------------------------------------------------------------------ checks

    def check_python_dependencies(self) -> CheckResult:
        missing = []
        for module, package in (("yaml", "python3-yaml"), ("gi", "python3-gi")):
            try:
                __import__(module)
            except Exception:  # noqa: BLE001
                missing.append(package)
        if missing:
            return CheckResult(
                "Environment", "Python dependencies", "FAIL",
                f"Missing modules: {', '.join(missing)}",
                fix=f"Install with your package manager: {' '.join(missing)}",
            )
        return CheckResult("Environment", "Python dependencies", "PASS",
                           "PyGObject and PyYAML are importable")

    def check_privileged_helper(self) -> CheckResult:
        if not HELPER_BIN.exists():
            return CheckResult(
                "Privilege Separation", "Privileged helper", "FAIL",
                f"Not found at {HELPER_BIN}",
                fix="Run: sudo ./scripts/install.sh --system",
            )
        try:
            info = HELPER_BIN.stat()
        except OSError as exc:
            return CheckResult("Privilege Separation", "Privileged helper", "FAIL",
                               f"Cannot stat helper: {exc}")
        if info.st_uid != 0 or info.st_gid != 0:
            return CheckResult(
                "Privilege Separation", "Privileged helper", "FAIL",
                f"Owned by uid {info.st_uid}/gid {info.st_gid}, must be root:root",
                fix=f"sudo chown root:root {HELPER_BIN}",
            )
        if not info.st_mode & 0o111:
            return CheckResult("Privilege Separation", "Privileged helper", "FAIL",
                               "Helper is not executable",
                               fix=f"sudo chmod 0755 {HELPER_BIN}")
        world_writable = bool(info.st_mode & 0o022)
        if world_writable:
            return CheckResult("Privilege Separation", "Privileged helper", "FAIL",
                               "Helper is group/other writable",
                               fix=f"sudo chmod 0755 {HELPER_BIN}")
        return CheckResult("Privilege Separation", "Privileged helper", "PASS",
                           f"Installed at {HELPER_BIN} (0755, root:root)")

    def check_helper_capabilities(self) -> CheckResult:
        """The installed helper must support the actions this version uses."""
        if not HELPER_BIN.exists():
            return CheckResult("Privilege Separation", "Helper capabilities", "WARN",
                               "Helper not installed; cannot verify actions")
        try:
            text = HELPER_BIN.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            return CheckResult("Privilege Separation", "Helper capabilities", "WARN",
                               f"Cannot read helper: {exc}")
        required = ["--apply-stdin", "--cgroup-init", "--cgroup-set", "--cgroup-clear", "--flush", "--status"]
        missing = [flag for flag in required if flag not in text]
        if missing:
            return CheckResult(
                "Privilege Separation", "Helper capabilities", "FAIL",
                f"Installed helper is outdated; missing: {', '.join(missing)}",
                fix="Reinstall the helper: sudo ./scripts/install.sh --system",
            )
        return CheckResult("Privilege Separation", "Helper capabilities", "PASS",
                           "All required helper actions are present")

    def check_sudoers_rule(self) -> CheckResult:
        if not HELPER_BIN.exists():
            return CheckResult("Privilege Separation", "Sudoers rule", "FAIL",
                               "Cannot test sudoers without the helper")
        try:
            start = time.perf_counter()
            res = subprocess.run(["sudo", "-n", str(HELPER_BIN), "--status"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, timeout=5, check=False)
            elapsed = (time.perf_counter() - start) * 1000
        except subprocess.TimeoutExpired:
            return CheckResult("Privilege Separation", "Sudoers rule", "FAIL",
                               "sudo prompted for a password (NOPASSWD missing)",
                               fix=f"sudo visudo -cf {SUDOERS_FILE}")
        except Exception as exc:  # noqa: BLE001
            return CheckResult("Privilege Separation", "Sudoers rule", "FAIL",
                               f"sudo check failed: {exc}")
        if res.returncode in (0, 1):
            return CheckResult("Privilege Separation", "Sudoers rule", "PASS",
                               f"Passwordless sudo verified ({elapsed:.1f} ms)")
        return CheckResult("Privilege Separation", "Sudoers rule", "FAIL",
                           f"sudo returned {res.returncode}: {res.stderr.strip()}",
                           fix=f"sudo visudo -cf {SUDOERS_FILE}")

    def check_nftables(self) -> CheckResult:
        nft = shutil.which("nft") or ("/usr/sbin/nft" if Path("/usr/sbin/nft").exists() else "")
        if not nft:
            return CheckResult("Firewall Isolation", "nftables", "FAIL",
                               "nft binary not found",
                               fix="Install nftables (apt install nftables)")
        manager = NftablesManager()
        state = manager.table_present()
        if state is None:
            return CheckResult("Firewall Isolation", "nftables", "WARN",
                               f"nft present at {nft}, but table state could not be read")
        state_str = "active" if state else "inactive"
        return CheckResult("Firewall Isolation", "nftables", "PASS",
                           f"nft present at {nft}; table inet data_guardian is {state_str}",
                           details="Only 'table inet data_guardian' is ever touched.")

    def check_strict_cgroups(self) -> CheckResult:
        if not Path("/sys/fs/cgroup").is_dir():
            return CheckResult("Firewall Isolation", "Strict-mode cgroups", "WARN",
                               "cgroup v2 not mounted; strict mode will use the port fallback")
        manager = NftablesManager()
        if not manager.can_apply_rules():
            return CheckResult("Firewall Isolation", "Strict-mode cgroups", "WARN",
                               "Cannot verify cgroups without helper access")
        ok = manager.ensure_cgroups()
        if ok:
            return CheckResult("Firewall Isolation", "Strict-mode cgroups", "PASS",
                               "data_guardian/{foreground,whitelist} cgroups are available")
        return CheckResult("Firewall Isolation", "Strict-mode cgroups", "WARN",
                           "Could not create cgroups; strict mode falls back to web/SSH ports")

    def check_daemon_and_ipc(self) -> CheckResult:
        svc_active = False
        try:
            res = subprocess.run(["systemctl", "is-active", "data-guardian.service"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, timeout=3, check=False)
            svc_active = res.stdout.strip() == "active"
        except Exception:  # noqa: BLE001
            svc_active = False

        pid, rss_mb = None, 0.0
        try:
            res = subprocess.run(["pgrep", "-f", "src.data_guardian.main daemon"],
                                 stdout=subprocess.PIPE, text=True, check=False)
            pids = res.stdout.split()
            if pids:
                pid = int(pids[0])
                ps = subprocess.run(["ps", "-p", str(pid), "-o", "rss="],
                                    stdout=subprocess.PIPE, text=True, check=False)
                rss_mb = (int(ps.stdout.strip() or 0)) / 1024.0
        except Exception:  # noqa: BLE001
            pass

        sock_path = get_socket_path()
        ping_ms = 0.0
        response = None
        if sock_path.exists():
            try:
                start = time.perf_counter()
                response = send_ipc_command({"action": "ping"}, timeout=2.0)
                ping_ms = (time.perf_counter() - start) * 1000
            except Exception:  # noqa: BLE001
                response = None
        ipc_ok = bool(response and response.get("status") == "ok")

        if not svc_active and not pid:
            return CheckResult("Daemon & IPC", "Background daemon", "FAIL",
                               "Daemon is not running",
                               fix="sudo systemctl start data-guardian.service")
        if not ipc_ok:
            return CheckResult("Daemon & IPC", "Background daemon", "WARN",
                               f"PID {pid} running but IPC unresponsive at {sock_path}",
                               fix="sudo systemctl restart data-guardian.service")

        status = send_ipc_command({"action": "get_status"})
        firewall_error = (status or {}).get("firewall_error")
        if firewall_error:
            return CheckResult("Daemon & IPC", "Background daemon", "FAIL",
                               f"Firewall error reported by daemon: {firewall_error}",
                               details=f"PID {pid}, RSS {rss_mb:.1f} MiB, IPC {ping_ms:.2f} ms")
        mem_status = "PASS" if rss_mb < 80.0 else "WARN"
        return CheckResult("Daemon & IPC", "Background daemon", mem_status,
                           f"PID {pid}, RSS {rss_mb:.1f} MiB, IPC roundtrip {ping_ms:.2f} ms",
                           details=f"Service: {'active' if svc_active else 'unmanaged'}")

    def check_database_integrity(self) -> CheckResult:
        db_path = self.config_mgr.db_path
        if not db_path.exists():
            return CheckResult("Data Storage", "SQLite database", "WARN",
                               f"Not created yet at {db_path}")
        try:
            with sqlite3.connect(db_path, timeout=5.0) as conn:
                integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
                if integrity != "ok":
                    return CheckResult("Data Storage", "SQLite database", "FAIL",
                                       f"Integrity check failed: {integrity}",
                                       details=f"Path: {db_path}")
                version = conn.execute("SELECT MAX(version) FROM schema_version;").fetchone()[0]
                nets = conn.execute("SELECT COUNT(*) FROM network_daily_usage;").fetchone()[0]
                sessions = conn.execute("SELECT COUNT(*) FROM sessions;").fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            return CheckResult("Data Storage", "SQLite database", "FAIL", f"Database error: {exc}")
        if version != CURRENT_SCHEMA_VERSION:
            return CheckResult("Data Storage", "SQLite database", "WARN",
                               f"Schema v{version}, expected v{CURRENT_SCHEMA_VERSION}",
                               details=f"{sessions} sessions, {nets} network-days")
        return CheckResult("Data Storage", "SQLite database", "PASS",
                           f"Integrity OK (v{version}): {sessions} sessions, {nets} network-days",
                           details=f"Path: {db_path}")

    def check_file_permissions(self) -> CheckResult:
        problems = []
        for path, expected in ((self.config_mgr.db_path, 0o600),
                               (self.config_mgr.config_path, 0o600)):
            if not path.exists():
                continue
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                problems.append(f"{path} is {oct(mode)} (want {oct(expected)})")
        if problems:
            return CheckResult("Data Storage", "File permissions", "FAIL",
                               "; ".join(problems),
                               fix="Run: chmod 600 " + " ".join(str(p) for p in problems))
        return CheckResult("Data Storage", "File permissions", "PASS",
                           "Database and config are readable only by the owner")

    def check_desktop_integration(self) -> CheckResult:
        icon_ok = ICON_FILE.exists()
        autostart = Path.home() / ".config" / "autostart" / "data-guardian-tray.desktop"
        autostart_ok = autostart.exists()
        try:
            res = subprocess.run(["pgrep", "-f", "src.data_guardian.main tray"],
                                 stdout=subprocess.PIPE, text=True, check=False)
            tray_running = bool(res.stdout.strip())
        except Exception:  # noqa: BLE001
            tray_running = False

        summary = (f"icon {'installed' if icon_ok else 'missing'}; "
                   f"tray {'running' if tray_running else 'inactive'}; "
                   f"autostart {'configured' if autostart_ok else 'disabled'}")
        status = "PASS" if (icon_ok and autostart_ok) else "WARN"
        fix = None if status == "PASS" else "sudo ./scripts/install.sh --system && ./scripts/install.sh"
        return CheckResult("Desktop Integration", "Tray & icons", status, summary, fix=fix)

    def check_network_and_hotspot(self) -> CheckResult:
        monitor = NetworkMonitor(
            designated_uuid=self.config_mgr.designated_hotspot_uuid,
            designated_name=self.config_mgr.designated_hotspot_name,
        )
        active = monitor.get_active_connection()
        if not active:
            return CheckResult("Network Monitor", "Active connection", "WARN",
                               "No active Wi-Fi or Ethernet connection",
                               details=f"Designated hotspot: {self.config_mgr.designated_hotspot_name or 'unset'}")
        is_hotspot = monitor.matches_designated(active)
        return CheckResult(
            "Network Monitor", "Active connection", "PASS",
            f"'{active.name}' on {active.interface} "
            f"[{'METERED' if is_hotspot else 'unmetered'}]",
            details=f"UUID {active.uuid}; designated hotspot "
                    f"{self.config_mgr.designated_hotspot_name or self.config_mgr.designated_hotspot_uuid or 'unset'}",
        )


# ------------------------------------------------------------------- reporting

_MARKS = {"PASS": ("\033[92m", "PASS"), "WARN": ("\033[93m", "WARN"), "FAIL": ("\033[91m", "FAIL")}


def format_report(results: List[CheckResult], color: bool = True) -> str:
    reset = "\033[0m" if color else ""
    bold = "\033[1m" if color else ""
    lines = [f"{bold}Linux Data Guardian — doctor{reset}", ""]
    for r in results:
        prefix, label = _MARKS.get(r.status, ("", r.status))
        tag = f"{prefix}[{label}]{reset}" if color else f"[{label}]"
        lines.append(f"{tag} {bold}{r.name}{reset} ({r.category})")
        lines.append(f"      {r.summary}")
        if r.details:
            lines.append(f"      {r.details}")
        if r.fix and r.status != "PASS":
            lines.append(f"      fix: {r.fix}")
        lines.append("")
    passed = sum(1 for r in results if r.status == "PASS")
    warned = sum(1 for r in results if r.status == "WARN")
    failed = sum(1 for r in results if r.status == "FAIL")
    lines.append(f"{bold}Summary:{reset} {passed} passed, {warned} warnings, {failed} failed")
    return "\n".join(lines)


def run_doctor(as_json: bool = False, color: bool = True) -> int:
    """Return a process exit code: 0 healthy, 2 warnings, 1 failures."""
    results = Doctor().run_all()
    if as_json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        print(format_report(results, color=color))
    if any(r.status == "FAIL" for r in results):
        return 1
    if any(r.status == "WARN" for r in results):
        return 2
    return 0
