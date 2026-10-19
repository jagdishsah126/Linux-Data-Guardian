"""Core background daemon for Linux Data Guardian."""

from __future__ import annotations
import os
import sys
import time
import signal
import subprocess
from typing import Any, Dict, Optional

from src.data_guardian.config import ConfigManager
from src.data_guardian.database import Database
from src.data_guardian.network.nm_monitor import NetworkMonitor, ConnectionInfo
from src.data_guardian.network.byte_counter import InterfaceByteCounter, format_bytes
from src.data_guardian.firewall.nft_manager import NftablesManager
from src.data_guardian.window_tracker.x11_tracker import X11WindowTracker
from src.data_guardian.ipc import IpcServer


class DataGuardianDaemon:
    """Orchestrates network detection, accounting, firewall policies, and IPC."""

    def __init__(self, config_manager: Optional[ConfigManager] = None) -> None:
        self.config_mgr = config_manager or ConfigManager()
        self.db = Database(self.config_mgr.db_path)
        self.monitor = NetworkMonitor(
            designated_uuid=self.config_mgr.designated_hotspot_uuid,
            designated_name=self.config_mgr.config.get("hotspot", {}).get("designated_name", "")
        )
        self.byte_counter = InterfaceByteCounter()
        self.firewall = NftablesManager()
        self.window_tracker = X11WindowTracker()
        self.ipc_server = IpcServer(handler=self._handle_ipc_request)

        self._active_session_id: Optional[int] = None
        self._is_running: bool = False
        self._current_profile: str = self.config_mgr.active_profile
        self._is_quota_blocked: bool = False
        self._active_app_name: str = "unknown"
        self._pending_notifications: List[Dict[str, str]] = []

        # Quota-state recovery on startup: check if today or this month already exceeded limits
        today_usage = self.db.get_today_usage()
        daily_limit = self.config_mgr.daily_limit_mb * 1024 * 1024
        monthly_limit = self.config_mgr.monthly_limit_mb * 1024 * 1024
        billing_day = self.config_mgr.config.get("quotas", {}).get("billing_cycle_day", 1)
        monthly_usage = self.db.get_monthly_usage(
            connection_uuid=self.config_mgr.designated_hotspot_uuid or None,
            billing_cycle_day=billing_day
        )
        is_daily_exceeded = (daily_limit > 0 and today_usage.get("total_bytes", 0) >= today_usage.get("effective_limit_bytes", daily_limit))
        is_monthly_exceeded = (monthly_limit > 0 and monthly_usage.get("total_bytes", 0) >= monthly_limit)
        if is_daily_exceeded or is_monthly_exceeded:
            self._is_quota_blocked = True
            print("[!] Quota limit already exceeded on startup. Enforcing quota-blocked state.")

    def start(self) -> None:
        """Starts daemon loops and signal handlers."""
        self._is_running = True
        self.ipc_server.start()

        # Signal handlers for graceful exit
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        print("[+] Linux Data Guardian Daemon started.")

        try:
            while self._is_running:
                self._iteration()
                interval = self.config_mgr.config.get("app", {}).get("poll_interval_seconds", 5)
                time.sleep(interval)
        finally:
            self.stop()

    def stop(self) -> None:
        """Gracefully shuts down daemon, flushes rules, and ends sessions."""
        self._is_running = False
        self.ipc_server.stop()
        if self._active_session_id:
            self.db.end_session(self._active_session_id)
            self._active_session_id = None

        # Clean firewall rules
        self.firewall.flush()
        print("[+] Linux Data Guardian Daemon stopped cleanly.")

    def _signal_handler(self, signum: int, frame: Any) -> None:
        self.stop()
        sys.exit(0)

    def _notify(self, title: str, message: str, urgency: str = "normal") -> None:
        """Sends desktop notification directly or queues for client UI pickup."""
        self._pending_notifications.append({"title": title, "message": message, "urgency": urgency})
        if os.environ.get("DISPLAY") and subprocess.run(["which", "notify-send"], stdout=subprocess.DEVNULL).returncode == 0:
            try:
                subprocess.run(["notify-send", "-u", urgency, title, message], check=False)
            except Exception:
                pass

    def _on_hotspot_activated(self, conn: ConnectionInfo) -> None:
        """Called when connected to the designated metered hotspot."""
        print(f"[+] Hotspot activated: {conn.name} ({conn.uuid}) on {conn.interface}")
        # Apply per-network profile if saved, fallback to default
        saved_profile = self.config_mgr.get_network_profile(conn.uuid)
        self._current_profile = saved_profile

        self._active_session_id = self.db.start_session(
            connection_uuid=conn.uuid,
            connection_name=conn.name,
            interface_name=conn.interface,
            profile=self._current_profile
        )
        self.byte_counter.reset_interface(conn.interface)

        # Apply profile firewall rules
        if self._current_profile != "off":
            success, msg = self.firewall.apply_profile(
                self._current_profile,
                interface=conn.interface,
                quota_exceeded=self._is_quota_blocked
            )
            if not success:
                print(f"[!] Warning: Firewall rule application failed: {msg}")

        self._notify(
            "Linux Data Guardian Active",
            f"Connected to {conn.name}. Data Saver ({self._current_profile.capitalize()}) is now active.",
            urgency="normal"
        )

    def _on_hotspot_deactivated(self) -> None:
        """Called when disconnected from metered hotspot or switched to normal Wi-Fi."""
        print("[-] Hotspot deactivated. Reverting to unmetered monitoring.")
        if self._active_session_id:
            self.db.end_session(self._active_session_id)
            self._active_session_id = None

        self.firewall.flush()
        self._is_quota_blocked = False

        self._notify(
            "Linux Data Guardian",
            "Switched to unrestricted network. All Data Saver restrictions have been lifted.",
            urgency="low"
        )

    def _iteration(self) -> None:
        """Single tick of the daemon loop."""
        # 1. Update hotspot monitor state
        is_hotspot = self.monitor.poll_check(
            on_hotspot_activated=self._on_hotspot_activated,
            on_hotspot_deactivated=self._on_hotspot_deactivated
        )

        active_conn = self.monitor.get_active_connection()
        if not active_conn:
            return

        # 2. Track active X11 window
        win_info = self.window_tracker.get_active_window()
        if win_info:
            self._active_app_name = win_info.app_name

        # 3. Calculate usage across active interface
        if active_conn.interface:
            delta_rx, delta_tx, speed = self.byte_counter.get_delta(active_conn.interface)

            if delta_rx > 0 or delta_tx > 0:
                # Update session
                if self._active_session_id:
                    self.db.update_session(self._active_session_id, delta_rx, delta_tx)

                # Daily quota calculation: quota limit is applied if connected to metered hotspot
                daily_limit_bytes = (self.config_mgr.daily_limit_mb * 1024 * 1024) if is_hotspot else 0
                usage = self.db.record_usage_delta(
                    delta_rx=delta_rx,
                    delta_tx=delta_tx,
                    quota_limit_bytes=daily_limit_bytes,
                    connection_uuid=active_conn.uuid,
                    connection_name=active_conn.name,
                )

                # Quota evaluation occurs only when on designated metered hotspot
                if is_hotspot and usage and usage.get("quota_limit_bytes", 0) > 0:
                    effective_limit = usage["effective_limit_bytes"]
                    total = usage["total_bytes"]
                    percent = (total / effective_limit) * 100 if effective_limit > 0 else 0

                    warning_pct = int(self.config_mgr.config.get("quotas", {}).get("warning_threshold_percent", 80))
                    if percent >= warning_pct and not usage.get("warning_fired", False):
                        self.db.set_warning_fired()
                        self._notify(
                            f"Data Warning ({warning_pct}% Reached)",
                            f"You have used {format_bytes(total)} of today's {format_bytes(effective_limit)} budget.",
                            urgency="critical"
                        )

                    # Daily quota exhaustion check
                    daily_exceeded = usage.get("quota_exceeded", False)

                    # Monthly quota exhaustion check
                    monthly_limit_bytes = self.config_mgr.monthly_limit_mb * 1024 * 1024
                    billing_day = self.config_mgr.config.get("quotas", {}).get("billing_cycle_day", 1)
                    monthly_usage = self.db.get_monthly_usage(
                        connection_uuid=self.config_mgr.designated_hotspot_uuid or None,
                        billing_cycle_day=billing_day
                    )
                    monthly_total = monthly_usage.get("total_bytes", 0)
                    monthly_exceeded = (monthly_limit_bytes > 0 and monthly_total >= monthly_limit_bytes)

                    if (daily_exceeded or monthly_exceeded) and not self._is_quota_blocked:
                        self._is_quota_blocked = True
                        reason = "Monthly" if monthly_exceeded else "Daily"
                        limit_str = format_bytes(monthly_limit_bytes if monthly_exceeded else effective_limit)
                        if self.config_mgr.config.get("quotas", {}).get("action_on_exhaustion") == "block_unapproved":
                            self.firewall.apply_profile(
                                self._current_profile,
                                interface=active_conn.interface,
                                quota_exceeded=True
                            )
                        self._notify(
                            f"{reason} Data Quota Exhausted!",
                            f"{reason} limit of {limit_str} reached. Further unapproved outbound traffic is paused.",
                            urgency="critical"
                        )

    def _handle_ipc_request(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """Handles commands sent by tray or dashboard UI."""
        action = req.get("action", "")

        if action == "ping":
            return {"status": "ok", "pong": True}

        if action == "get_status":
            today_usage = self.db.get_today_usage()
            active_conn = self.monitor.get_active_connection()
            is_hotspot = self.monitor.is_designated_hotspot_active()
            billing_day = self.config_mgr.config.get("quotas", {}).get("billing_cycle_day", 1)
            monthly_usage = self.db.get_monthly_usage(
                connection_uuid=self.config_mgr.designated_hotspot_uuid or None,
                billing_cycle_day=billing_day
            )
            notifications = list(self._pending_notifications)
            self._pending_notifications.clear()
            return {
                "status": "ok",
                "is_hotspot_active": is_hotspot,
                "connection_name": active_conn.name if active_conn else "Disconnected",
                "connection_uuid": active_conn.uuid if active_conn else "",
                "interface": active_conn.interface if active_conn else "",
                "current_profile": self._current_profile,
                "is_quota_blocked": self._is_quota_blocked,
                "today_rx": today_usage.get("rx_bytes", 0),
                "today_tx": today_usage.get("tx_bytes", 0),
                "today_total": today_usage.get("total_bytes", 0),
                "daily_limit_bytes": today_usage.get("effective_limit_bytes", 0),
                "monthly_total": monthly_usage.get("total_bytes", 0),
                "monthly_limit_bytes": self.config_mgr.monthly_limit_mb * 1024 * 1024,
                "active_app": self._active_app_name,
                "notifications": notifications,
            }

        elif action == "set_profile":
            new_profile = req.get("profile", "balanced")
            if new_profile in ("balanced", "strict", "off"):
                self._current_profile = new_profile
                self.config_mgr.active_profile = new_profile
                active_conn = self.monitor.get_active_connection()
                if active_conn:
                    self.config_mgr.set_network_profile(active_conn.uuid, new_profile)
                if self.monitor.is_designated_hotspot_active():
                    self.firewall.apply_profile(
                        new_profile,
                        interface=(active_conn.interface if active_conn else ""),
                        quota_exceeded=self._is_quota_blocked
                    )
                self._notify(
                    "Data Guardian Profile",
                    f"Profile switched to {new_profile.upper()} mode.",
                    urgency="normal"
                )
                return {"status": "ok", "profile": new_profile}
            return {"status": "error", "message": "Invalid profile"}

        elif action == "grant_allowance":
            mb = int(req.get("mb", 100))
            allowance_bytes = mb * 1024 * 1024
            self.db.grant_temporary_allowance(allowance_bytes)
            self._is_quota_blocked = False
            active_conn = self.monitor.get_active_connection()
            if self.monitor.is_designated_hotspot_active():
                self.firewall.apply_profile(
                    self._current_profile,
                    interface=(active_conn.interface if active_conn else ""),
                    quota_exceeded=False
                )
            self._notify("Data Allowance Granted", f"Added +{mb} MB to today's quota.", urgency="normal")
            return {"status": "ok", "added_mb": mb}

        elif action == "allow_apt_window":
            active_conn = self.monitor.get_active_connection()
            iface = active_conn.interface if active_conn else ""
            ok, msg = self.firewall.open_apt_window(iface)
            return {"status": "ok" if ok else "error", "message": msg}

        elif action == "emergency_bypass":
            self.firewall.flush()
            self._current_profile = "off"
            self.config_mgr.active_profile = "off"
            self._is_quota_blocked = False
            self._notify("Emergency Bypass", "All network restrictions have been removed.", urgency="critical")
            return {"status": "ok", "message": "Emergency bypass applied"}

        elif action == "set_hotspot_uuid":
            uuid = req.get("uuid", "")
            self.config_mgr.designated_hotspot_uuid = uuid
            self.monitor.designated_uuid = uuid
            return {"status": "ok", "designated_uuid": uuid}

        return {"status": "error", "message": f"Unknown action: {action}"}


def run_daemon() -> None:
    daemon = DataGuardianDaemon()
    daemon.start()


if __name__ == "__main__":
    run_daemon()
