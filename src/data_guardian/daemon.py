"""Core background daemon for Linux Data Guardian.

Concurrency model
-----------------
* The main loop owns the network monitor, the firewall and all state mutation.
* IPC handlers validate a request and mutate state under ``self._lock``; they never
  touch the monitor concurrently with the loop.
* UI clients read an immutable status snapshot built once per tick, plus a
  non-destructive notification feed with a client-supplied cursor.
"""

from __future__ import annotations

import fcntl
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.data_guardian.config import ConfigManager
from src.data_guardian.database import Database
from src.data_guardian.firewall.nft_manager import NftablesManager
from src.data_guardian.ipc import IpcServer, get_runtime_dir
from src.data_guardian.network.byte_counter import InterfaceByteCounter, format_bytes
from src.data_guardian.network.nm_monitor import ConnectionInfo, NetworkMonitor
from src.data_guardian.processes import descendant_pids, find_pids_by_names
from src.data_guardian.quota import QuotaVerdict, evaluate, newly_fired_thresholds
from src.data_guardian.window_tracker.x11_tracker import X11WindowTracker

MB = 1024 * 1024
APT_WINDOW_SECONDS = 15 * 60
MAINTENANCE_EVERY_TICKS = 60  # ~5 minutes at the default 5s interval


class DataGuardianDaemon:
    """Orchestrates network detection, accounting, firewall policies, and IPC."""

    def __init__(
        self,
        config_manager: Optional[ConfigManager] = None,
        *,
        use_instance_guard: bool = True,
    ) -> None:
        self.config_mgr = config_manager or ConfigManager()
        self.db = Database(
            self.config_mgr.db_path,
            designated_uuid=self.config_mgr.designated_hotspot_uuid,
        )
        self.monitor = NetworkMonitor(
            designated_uuid=self.config_mgr.designated_hotspot_uuid,
            designated_name=self.config_mgr.config.get("hotspot", {}).get("designated_name", ""),
        )
        self.byte_counter = InterfaceByteCounter()
        self.firewall = NftablesManager()
        self.window_tracker = X11WindowTracker()
        self.ipc_server = IpcServer(handler=self._handle_ipc_request)

        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._is_running = False
        self._stopped = False
        self._active_session_id: Optional[int] = None
        self._current_profile: str = self.config_mgr.active_profile
        self._is_hotspot_active = False
        self._active_app_name = "unknown"
        self._active_app_pid: Optional[int] = None
        self._apt_window_until: Optional[float] = None
        self._firewall_error: Optional[str] = None
        self._last_fg_pids: frozenset = frozenset()
        self._last_whitelist_pids: frozenset = frozenset()
        self._verdict: QuotaVerdict = evaluate(self.db, self.config_mgr)
        self._is_quota_blocked: bool = self._verdict.blocked
        self._ticks = 0
        self._instance_fd: Optional[int] = None
        self._use_instance_guard = use_instance_guard
        self._snapshot: Dict[str, Any] = {}

        # Recover from an unclean shutdown: no session may stay open forever.
        closed = self.db.close_orphan_sessions()
        if closed:
            print(f"[i] Closed {closed} orphaned session(s) from a previous run.")

        if self._verdict.reason:
            print(
                f"[!] Quota already exceeded ({self._verdict.reason}): "
                f"used {format_bytes(self._verdict.daily_used)}, "
                f"limit {format_bytes(self._verdict.daily_effective)}."
            )
        self._refresh_snapshot()

    # ---------------------------------------------------------------- lifecycle

    def acquire_instance_lock(self) -> bool:
        """Prevent two daemons from fighting over the same socket and firewall."""
        if not self._use_instance_guard:
            return True
        lock_path = get_runtime_dir() / "data-guardian.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            self._instance_fd = fd
            return True
        except OSError:
            return False

    def start(self) -> None:
        if not self.acquire_instance_lock():
            print("[!] Another data-guardian daemon is already running. Exiting.")
            return

        self._is_running = True
        X11WindowTracker.autodetect_environment()
        self.ipc_server.start()

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        print("[+] Linux Data Guardian Daemon started.")
        try:
            while self._is_running:
                self._iteration()
                self._ticks += 1
                self._wake.wait(self.config_mgr.poll_interval_seconds)
                self._wake.clear()
        finally:
            self.stop()

    def stop(self) -> None:
        """Idempotent graceful shutdown: flush rules and close the active session."""
        if self._stopped:
            return
        self._stopped = True
        self._is_running = False
        self._wake.set()

        self.ipc_server.stop()
        with self._lock:
            if self._active_session_id:
                self.db.end_session(self._active_session_id)
                self._active_session_id = None
            ok, msg = self.firewall.flush()
            if not ok:
                print(f"[!] Warning during shutdown flush: {msg}")
            self.db.checkpoint()

        if self._instance_fd is not None:
            try:
                os.close(self._instance_fd)
            except OSError:
                pass
            self._instance_fd = None
        print("[+] Linux Data Guardian Daemon stopped cleanly.")

    def _signal_handler(self, signum: int, frame: Any) -> None:
        self._is_running = False
        self._wake.set()
        try:
            self.stop()
        finally:
            sys.exit(0)

    # ------------------------------------------------------------- notifications

    def _notify(self, title: str, message: str, urgency: str = "normal") -> None:
        """Persist a notification for UI clients (non-destructive, deduplicated by id)."""
        try:
            self.db.add_notification(title, message, urgency)
        except Exception as exc:  # noqa: BLE001 - notifications must never crash the loop
            print(f"[!] Failed to record notification: {exc}")
        self._wake.set()

    # ------------------------------------------------------------- transitions

    def _on_hotspot_activated(self, conn: ConnectionInfo) -> None:
        print(f"[+] Hotspot activated: {conn.name} ({conn.uuid}) on {conn.interface}")

        # Never leave a previous session dangling.
        if self._active_session_id:
            self.db.end_session(self._active_session_id)
            self._active_session_id = None

        self._current_profile = self.config_mgr.get_network_profile(conn.uuid)
        self._active_session_id = self.db.start_session(
            connection_uuid=conn.uuid,
            connection_name=conn.name,
            interface_name=conn.interface,
            profile=self._current_profile,
        )
        self.byte_counter.reset_interface(conn.interface)
        self._last_fg_pids = frozenset()
        self._last_whitelist_pids = frozenset()

        self._recompute_quota()
        self._apply_firewall(conn.interface, reason="hotspot activated")

        self._notify(
            "Linux Data Guardian Active",
            f"Connected to {conn.name}. Data Saver ({self._current_profile.capitalize()}) is now active.",
        )

    def _on_hotspot_deactivated(self) -> None:
        print("[-] Hotspot deactivated. Reverting to unmetered monitoring.")
        if self._active_session_id:
            self.db.end_session(self._active_session_id)
            self._active_session_id = None

        for group in ("foreground", "whitelist"):
            clear_ok, clear_msg = self.firewall.cgroup_set(group, [])
            if not clear_ok:
                print(f"[i] Could not clear {group} cgroup: {clear_msg}")
        self._last_fg_pids = frozenset()
        self._last_whitelist_pids = frozenset()
        self._apt_window_until = None

        ok, msg = self.firewall.flush()
        if not ok:
            self._firewall_error = msg
            print(f"[!] Failed to lift restrictions: {msg}")
        else:
            self._firewall_error = None
        self._is_quota_blocked = False

        self._notify(
            "Linux Data Guardian",
            "Switched to unrestricted network. All Data Saver restrictions have been lifted.",
            urgency="low",
        )

    # ---------------------------------------------------------------- firewall

    @property
    def _apt_window_active(self) -> bool:
        return self._apt_window_until is not None and time.monotonic() < self._apt_window_until

    def _apply_firewall(self, interface: str, reason: str = "") -> bool:
        """Single writer for firewall changes. Records any failure in status."""
        with self._lock:
            profile = self._current_profile
            if profile == "off" and not self._is_quota_blocked and not self._apt_window_active:
                ok, msg = self.firewall.flush()
            else:
                effective_profile = profile if profile != "off" else "balanced"
                ok, msg = self.firewall.apply_profile(
                    effective_profile,
                    interface=interface,
                    quota_exceeded=self._is_quota_blocked,
                    apt_window=self._apt_window_active,
                    extra_tcp_ports=self.config_mgr.strict_allowed_ports_tcp,
                )
            if ok:
                if self._firewall_error:
                    self._notify("Data Guardian", "Firewall protection restored.", "normal")
                self._firewall_error = None
            else:
                self._firewall_error = msg
                print(f"[!] Firewall error ({reason or 'apply'}): {msg}")
                self._notify(
                    "Data Guardian protection FAILED",
                    f"Could not apply firewall rules: {msg}",
                    urgency="critical",
                )
            self._refresh_snapshot()
            return ok

    def _recompute_quota(self) -> QuotaVerdict:
        """Recompute the quota verdict; return the new state."""
        with self._lock:
            verdict = evaluate(self.db, self.config_mgr)
            self._verdict = verdict
            self._is_quota_blocked = verdict.blocked
            return verdict

    # --------------------------------------------------------- cgroup supervision

    def _sync_foreground_cgroup(self, interface: str) -> None:
        """Move the focused application's process tree into the foreground cgroup."""
        if not self._is_hotspot_active or self._current_profile != "strict" or self._is_quota_blocked:
            return
        pid = self._active_app_pid
        pids = frozenset(descendant_pids(pid)) if pid else frozenset()
        # Compare complete process trees, not just the focused PID: a browser
        # spawns renderer children that must also be allowed. A lost focus
        # clears the group so the previously focused app cannot keep access.
        if pids == self._last_fg_pids:
            return
        ok, msg = self.firewall.cgroup_set("foreground", sorted(pids))
        if ok:
            self._last_fg_pids = pids
        else:
            self._last_fg_pids = frozenset()
            print(f"[i] foreground cgroup update failed: {msg}")

    def _sync_whitelist_cgroup(self, interface: str) -> None:
        """Keep the whitelisted CLI tools (opencode, antigravity) in their cgroup."""
        if not self._is_hotspot_active or self._current_profile != "strict":
            return
        names = self.config_mgr.strict_whitelist_processes
        groups = find_pids_by_names(names)
        # Include every descendant of a whitelisted tool (shells, language
        # servers, child workers), not only the parent process.
        pids = frozenset(
            child
            for group in groups.values()
            for root in group
            for child in descendant_pids(root)
        )
        if pids == self._last_whitelist_pids:
            return
        ok, msg = self.firewall.cgroup_set("whitelist", sorted(pids))
        if ok:
            self._last_whitelist_pids = pids
        else:
            print(f"[i] whitelist cgroup update failed: {msg}")

    # ------------------------------------------------------------------- loop

    def _iteration(self) -> None:
        is_hotspot = self.monitor.poll_check(
            on_hotspot_activated=self._on_hotspot_activated,
            on_hotspot_deactivated=self._on_hotspot_deactivated,
        )
        self._is_hotspot_active = is_hotspot
        active_conn = self.monitor.get_active_connection()

        # 1. Expire the APT window.
        if self._apt_window_until is not None and not self._apt_window_active:
            self._apt_window_until = None
            if active_conn and active_conn.interface:
                self._apply_firewall(active_conn.interface, reason="apt window closed")
            self._notify("Data Guardian", "APT download window closed.", urgency="low")

        if not active_conn:
            self._refresh_snapshot()
            return

        # 2. Foreground window. A system service may start before the desktop
        # session exists, so retry discovery periodically instead of giving up
        # forever after the first failed start.
        if self._ticks % 12 == 0 and not self.window_tracker.is_x11_available():
            X11WindowTracker.autodetect_environment()
        win = self.window_tracker.get_active_window()
        if win:
            self._active_app_name = win.app_name or "unknown"
            if win.pid and win.pid != self._active_app_pid:
                self._active_app_pid = win.pid
                self._last_fg_pids = frozenset()

        if is_hotspot and self._current_profile == "strict":
            self._sync_foreground_cgroup(active_conn.interface)
            self._sync_whitelist_cgroup(active_conn.interface)

        # 3. Traffic accounting.
        if active_conn.interface:
            delta_rx, delta_tx, _speed = self.byte_counter.get_delta(active_conn.interface)
            if delta_rx > 0 or delta_tx > 0:
                if self._active_session_id:
                    self.db.update_session(self._active_session_id, delta_rx, delta_tx)
                self.db.record_usage_delta(
                    delta_rx=delta_rx,
                    delta_tx=delta_tx,
                    metered=bool(is_hotspot),
                    daily_limit_bytes=self.config_mgr.daily_limit_mb * MB,
                    connection_uuid=active_conn.uuid,
                    connection_name=active_conn.name,
                )
                self._evaluate_and_enforce(active_conn.interface)

        # 4. Periodic maintenance.
        if self._ticks % MAINTENANCE_EVERY_TICKS == 0 and self._ticks > 0:
            self._maintenance()

        self._refresh_snapshot()

    def _evaluate_and_enforce(self, interface: str) -> None:
        verdict = evaluate(self.db, self.config_mgr)

        # Warnings (once per threshold per day).
        if self._is_hotspot_active:
            for threshold in newly_fired_thresholds(self.db, self.config_mgr, verdict):
                fired = self.db.mark_warning_fired(threshold)
                if fired:
                    self._notify(
                        f"Data Warning ({threshold}% Reached)",
                        f"You have used {format_bytes(verdict.daily_used)} of today's "
                        f"{format_bytes(verdict.daily_effective)} budget.",
                        urgency="critical",
                    )

        changed = verdict.blocked != self._is_quota_blocked
        self._verdict = verdict
        self._is_quota_blocked = verdict.blocked

        if changed:
            self._apply_firewall(interface, reason="quota state changed")
            if verdict.blocked:
                limit = (verdict.monthly_limit if verdict.reason == "monthly"
                         else verdict.daily_effective)
                self._notify(
                    f"{verdict.reason.capitalize()} Data Quota Exhausted!",
                    f"{verdict.reason.capitalize()} limit of {format_bytes(limit)} reached. "
                    "Further outbound traffic is paused.",
                    urgency="critical",
                )

    def _maintenance(self) -> None:
        try:
            self.db.prune_notifications(24)
            retention = self.config_mgr.history_retention_days
            if retention:
                self.db.prune_history(retention)
            self.db.checkpoint()
        except Exception as exc:  # noqa: BLE001
            print(f"[i] maintenance failed: {exc}")

    # ---------------------------------------------------------------- status

    def _refresh_snapshot(self) -> None:
        """Rebuild the immutable status snapshot served to UI clients."""
        try:
            today = self.db.get_today_usage()
            monthly = self.db.get_monthly_usage(billing_cycle_day=self.config_mgr.billing_cycle_day)
            active_conn = None
            try:
                active_conn = self.monitor.get_active_connection()
            except Exception:  # noqa: BLE001
                active_conn = None

            snapshot = {
                "status": "ok",
                "is_hotspot_active": self._is_hotspot_active,
                "connection_name": active_conn.name if active_conn else "Disconnected",
                "connection_uuid": active_conn.uuid if active_conn else "",
                "connection_interface": active_conn.interface if active_conn else "",
                "current_profile": self._current_profile,
                "is_quota_blocked": self._is_quota_blocked,
                "quota_reason": self._verdict.reason if self._verdict else None,
                "today_rx": today.get("rx_bytes", 0),
                "today_tx": today.get("tx_bytes", 0),
                "today_total": today.get("total_bytes", 0),
                "metered_today": self._verdict.daily_used if self._verdict else 0,
                "daily_limit_bytes": today.get("effective_limit_bytes", 0),
                "daily_limit_configured": self._verdict.daily_limit if self._verdict else 0,
                "monthly_total": monthly.get("total_bytes", 0),
                "monthly_limit_bytes": self.config_mgr.monthly_limit_mb * MB,
                "active_app": self._active_app_name,
                "apt_window_remaining": (
                    int(self._apt_window_until - time.monotonic())
                    if self._apt_window_active and self._apt_window_until else 0
                ),
                "firewall_error": self._firewall_error,
                "latest_notification_id": self.db.latest_notification_id(),
                "pid": os.getpid(),
            }
        except Exception as exc:  # noqa: BLE001
            snapshot = {"status": "error", "message": str(exc)}
        with self._lock:
            self._snapshot = snapshot

    # ------------------------------------------------------------------ IPC

    def _handle_ipc_request(self, req: Dict[str, Any]) -> Dict[str, Any]:
        action = req.get("action", "")

        if action == "ping":
            return {"status": "ok", "pong": True, "version": 2}

        with self._lock:
            if action == "get_status":
                since = int(req.get("since_id", 0) or 0)
                snapshot = dict(self._snapshot)
                # Notification cursor data must be live, not from the last tick.
                snapshot["latest_notification_id"] = self.db.latest_notification_id()
                snapshot["notifications"] = self.db.get_notifications_since(since)
                return snapshot

            if action == "set_profile":
                return self._cmd_set_profile(req)

            if action == "set_network_profile":
                return self._cmd_set_network_profile(req)

            if action == "grant_allowance":
                return self._cmd_grant_allowance(req)

            if action == "allow_apt_window":
                return self._cmd_allow_apt_window(req)

            if action == "close_apt_window":
                return self._cmd_close_apt_window(req)

            if action == "emergency_bypass":
                return self._cmd_emergency_bypass()

            if action == "set_hotspot_uuid":
                return self._cmd_set_hotspot_uuid(req)

            if action == "report_focus":
                return self._cmd_report_focus(req)

        return {"status": "error", "message": f"Unknown action: {action}"}

    def _active_interface(self) -> str:
        conn = self.monitor.get_active_connection()
        return conn.interface if conn and conn.interface else ""

    def _cmd_set_profile(self, req: Dict[str, Any]) -> Dict[str, Any]:
        new_profile = str(req.get("profile", "balanced"))
        if new_profile not in ("balanced", "strict", "off"):
            return {"status": "error", "message": "Invalid profile"}
        self._current_profile = new_profile
        self.config_mgr.active_profile = new_profile
        conn = self.monitor.get_active_connection()
        if conn:
            self.config_mgr.set_network_profile(conn.uuid, new_profile)
        interface = conn.interface if conn else ""
        if self._is_hotspot_active and interface:
            self._apply_firewall(interface, reason="profile change")
        self._refresh_snapshot()
        self._notify("Data Guardian Profile", f"Profile switched to {new_profile.upper()} mode.")
        return {"status": "ok", "profile": new_profile}

    def _cmd_set_network_profile(self, req: Dict[str, Any]) -> Dict[str, Any]:
        uuid = str(req.get("uuid", ""))
        profile = str(req.get("profile", ""))
        if not uuid or profile not in ("balanced", "strict", "off"):
            return {"status": "error", "message": "uuid and a valid profile are required"}
        self.config_mgr.set_network_profile(uuid, profile)
        conn = self.monitor.get_active_connection()
        if conn and conn.uuid == uuid:
            self._current_profile = profile
            if self._is_hotspot_active and conn.interface:
                self._apply_firewall(conn.interface, reason="network profile change")
        self._refresh_snapshot()
        return {"status": "ok", "uuid": uuid, "profile": profile}

    def _cmd_grant_allowance(self, req: Dict[str, Any]) -> Dict[str, Any]:
        try:
            mb = int(req.get("mb", 100))
        except (TypeError, ValueError):
            return {"status": "error", "message": "mb must be an integer"}
        if mb <= 0 or mb > self.config_mgr.max_allowance_mb:
            return {"status": "error",
                    "message": f"mb must be between 1 and {self.config_mgr.max_allowance_mb}"}
        self.db.grant_temporary_allowance(mb * MB)
        self._recompute_quota()
        conn = self.monitor.get_active_connection()
        if self._is_hotspot_active and conn and conn.interface:
            self._apply_firewall(conn.interface, reason="allowance granted")
        self._refresh_snapshot()
        self._notify("Data Allowance Granted", f"Added +{mb} MB to today's quota.")
        return {"status": "ok", "added_mb": mb}

    def _cmd_allow_apt_window(self, req: Dict[str, Any]) -> Dict[str, Any]:
        if not self._is_hotspot_active:
            return {"status": "error", "message": "Not on the metered hotspot"}
        interface = self._active_interface()
        if not interface:
            return {"status": "error", "message": "No active interface"}
        minutes = int(req.get("minutes", 15) or 15)
        minutes = max(1, min(minutes, 120))
        self._apt_window_until = time.monotonic() + minutes * 60
        ok = self._apply_firewall(interface, reason="apt window opened")
        self._refresh_snapshot()
        if not ok:
            return {"status": "error", "message": self._firewall_error or "apply failed"}
        self._notify("APT Window Open", f"APT downloads allowed for {minutes} minute(s).")
        return {"status": "ok", "minutes": minutes}

    def _cmd_close_apt_window(self, req: Dict[str, Any]) -> Dict[str, Any]:
        self._apt_window_until = None
        interface = self._active_interface()
        if self._is_hotspot_active and interface:
            self._apply_firewall(interface, reason="apt window closed")
        self._refresh_snapshot()
        return {"status": "ok"}

    def _cmd_emergency_bypass(self) -> Dict[str, Any]:
        self._apt_window_until = None
        self._current_profile = "off"
        self.config_mgr.active_profile = "off"
        self._is_quota_blocked = False
        self.db.set_quota_exceeded(False, None)
        ok, msg = self.firewall.flush()
        self._firewall_error = None if ok else msg
        self._refresh_snapshot()
        self._notify("Emergency Bypass", "All network restrictions have been removed.", urgency="critical")
        return {"status": "ok" if ok else "error", "message": "Emergency bypass applied" if ok else msg}

    def _cmd_set_hotspot_uuid(self, req: Dict[str, Any]) -> Dict[str, Any]:
        uuid = str(req.get("uuid", ""))
        name = str(req.get("name", ""))
        self.config_mgr.set_hotspot(uuid, name)
        self.monitor.designated_uuid = uuid
        if name:
            self.monitor.designated_name = name
        # Re-evaluate immediately: the previous activation state may no longer apply.
        self.monitor.invalidate()
        self._refresh_snapshot()
        return {"status": "ok", "designated_uuid": uuid, "designated_name": name}

    def _cmd_report_focus(self, req: Dict[str, Any]) -> Dict[str, Any]:
        app = str(req.get("app_name", "") or "unknown")
        pid = req.get("pid")
        try:
            pid = int(pid) if pid else None
        except (TypeError, ValueError):
            pid = None
        if app != self._active_app_name or pid != self._active_app_pid:
            self._active_app_name = app
            self._active_app_pid = pid
            self._last_fg_pids = frozenset()
        self._refresh_snapshot()
        return {"status": "ok"}


def run_daemon() -> None:
    daemon = DataGuardianDaemon()
    daemon.start()


if __name__ == "__main__":
    run_daemon()
