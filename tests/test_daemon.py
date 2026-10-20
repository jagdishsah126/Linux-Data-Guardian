"""IPC and daemon behaviour (regression tests for BUG-01/05/12/13/14/16/39/40)."""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.data_guardian.daemon import APT_WINDOW_SECONDS, DataGuardianDaemon
from src.data_guardian.ipc import IpcServer, get_runtime_dir, get_socket_path, send_ipc_command
from tests.support import MB, IsolatedTestCase


class DaemonHarness(IsolatedTestCase):
    def make_daemon(self, **conn_kwargs):
        conn = MagicMock()
        conn.uuid = conn_kwargs.get("uuid", "hot-uuid")
        conn.name = conn_kwargs.get("name", "Hotspot")
        conn.interface = conn_kwargs.get("interface", "wlan0")
        conn.is_metered = True

        patches = [
            patch("src.data_guardian.daemon.NetworkMonitor"),
            patch("src.data_guardian.daemon.NftablesManager"),
            patch("src.data_guardian.daemon.X11WindowTracker"),
            patch("src.data_guardian.daemon.IpcServer"),
        ]
        started = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in patches])

        monitor = started[0].return_value
        monitor.get_active_connection.return_value = conn
        monitor.poll_check.return_value = True
        monitor.is_designated_hotspot_active.return_value = True

        firewall = started[1].return_value
        firewall.apply_profile.return_value = (True, "ok")
        firewall.flush.return_value = (True, "ok")
        firewall.cgroup_set.return_value = (True, "ok")

        tracker = started[2].return_value
        tracker.get_active_window.return_value = None

        daemon = DataGuardianDaemon(config_manager=self.config, use_instance_guard=False)
        daemon._active_app_pid = None
        # The mocked monitor reports the hotspot as active; mirror what the loop
        # would have recorded after its first tick.
        daemon._is_hotspot_active = True
        return daemon


class TestDaemonStartup(DaemonHarness):
    def test_empty_db_does_not_block_on_startup(self):
        """BUG-01: the daemon must not start in 'quota exceeded' state."""
        daemon = self.make_daemon()
        self.assertFalse(daemon._is_quota_blocked)
        self.assertIsNone(daemon._verdict.reason)

    def test_startup_detects_real_exhaustion(self):
        self.record(1200, metered=True)
        daemon = self.make_daemon()
        self.assertTrue(daemon._is_quota_blocked)
        self.assertEqual(daemon._verdict.reason, "daily")


class TestDaemonQuotaCommands(DaemonHarness):
    def test_grant_allowance_on_empty_day(self):
        """BUG-02: the UI button must work before any traffic is recorded."""
        daemon = self.make_daemon()
        resp = daemon._handle_ipc_request({"action": "grant_allowance", "mb": 100})
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(self.db.get_quota_state()["allowance_bytes"], 100 * MB)

    def test_grant_allowance_rejects_bad_values(self):
        daemon = self.make_daemon()
        for bad in (0, -100, 10 ** 9, "abc"):
            resp = daemon._handle_ipc_request({"action": "grant_allowance", "mb": bad})
            self.assertEqual(resp["status"], "error", bad)

    def test_allowance_lifts_block_and_reapplies(self):
        self.record(1200, metered=True)
        daemon = self.make_daemon()
        daemon._is_quota_blocked = True
        daemon._handle_ipc_request({"action": "grant_allowance", "mb": 500})
        self.assertFalse(daemon._is_quota_blocked)
        daemon.firewall.apply_profile.assert_called()
        last = daemon.firewall.apply_profile.call_args
        self.assertFalse(last.kwargs.get("quota_exceeded", False))


class TestAptWindow(DaemonHarness):
    def test_window_opens_with_timer(self):
        daemon = self.make_daemon()
        resp = daemon._handle_ipc_request({"action": "allow_apt_window", "minutes": 15})
        self.assertEqual(resp["status"], "ok")
        self.assertTrue(daemon._apt_window_active)
        self.assertLessEqual(daemon._apt_window_until, time.monotonic() + APT_WINDOW_SECONDS + 1)

    def test_window_expires_and_reapplies(self):
        """BUG-05: the window must close by itself."""
        daemon = self.make_daemon()
        daemon._handle_ipc_request({"action": "allow_apt_window", "minutes": 15})
        daemon.firewall.apply_profile.reset_mock()
        daemon._apt_window_until = time.monotonic() - 1
        daemon._iteration()
        self.assertIsNone(daemon._apt_window_until)
        daemon.firewall.apply_profile.assert_called()
        self.assertFalse(daemon.firewall.apply_profile.call_args.kwargs.get("apt_window"))

    def test_window_rejected_off_hotspot(self):
        daemon = self.make_daemon()
        daemon._is_hotspot_active = False
        resp = daemon._handle_ipc_request({"action": "allow_apt_window"})
        self.assertEqual(resp["status"], "error")


class TestCgroupSupervision(DaemonHarness):
    def _strict_daemon(self):
        daemon = self.make_daemon()
        daemon._current_profile = "strict"
        daemon._is_hotspot_active = True
        daemon._is_quota_blocked = False
        return daemon

    def test_foreground_moves_new_children(self):
        daemon = self._strict_daemon()
        daemon._active_app_pid = 100
        with patch("src.data_guardian.daemon.descendant_pids",
                   side_effect=[{100, 101}, {100, 101, 102}, {100, 101, 102}]):
            daemon._sync_foreground_cgroup("wlan0")
            daemon._sync_foreground_cgroup("wlan0")
            daemon._sync_foreground_cgroup("wlan0")
        self.assertEqual(daemon.firewall.cgroup_set.call_count, 2)
        self.assertEqual(
            daemon.firewall.cgroup_set.call_args.args, ("foreground", [100, 101, 102])
        )

    def test_lost_focus_clears_foreground_group(self):
        daemon = self._strict_daemon()
        daemon._active_app_pid = None
        daemon._last_fg_pids = frozenset({100})
        daemon._sync_foreground_cgroup("wlan0")
        daemon.firewall.cgroup_set.assert_called_once_with("foreground", [])

    def test_whitelist_includes_descendants(self):
        daemon = self._strict_daemon()
        with patch("src.data_guardian.daemon.find_pids_by_names",
                   return_value={"opencode": {200}}), \
             patch("src.data_guardian.daemon.descendant_pids",
                   return_value={200, 201}):
            daemon._sync_whitelist_cgroup("wlan0")
        daemon.firewall.cgroup_set.assert_called_once_with("whitelist", [200, 201])


class TestProfileCommands(DaemonHarness):
    def test_set_network_profile_is_scoped(self):
        """BUG-29: setting another network's profile must not change the active one."""
        daemon = self.make_daemon()
        resp = daemon._handle_ipc_request(
            {"action": "set_network_profile", "uuid": "other-uuid", "profile": "strict"}
        )
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(self.config.get_network_profile("other-uuid"), "strict")
        self.assertEqual(daemon._current_profile, "balanced")

    def test_set_profile_applies_to_active_network(self):
        daemon = self.make_daemon()
        daemon._is_hotspot_active = True
        daemon._handle_ipc_request({"action": "set_profile", "profile": "strict"})
        self.assertEqual(daemon._current_profile, "strict")
        self.assertEqual(self.config.get_network_profile("hot-uuid"), "strict")
        self.assertEqual(
            daemon.firewall.apply_profile.call_args.kwargs["extra_tcp_ports"],
            self.config.strict_allowed_ports_tcp,
        )

    def test_invalid_profile_rejected(self):
        daemon = self.make_daemon()
        resp = daemon._handle_ipc_request({"action": "set_profile", "profile": "nope"})
        self.assertEqual(resp["status"], "error")

    def test_set_hotspot_reevaluates(self):
        """BUG-16: changing the designation must invalidate the monitor state."""
        daemon = self.make_daemon()
        resp = daemon._handle_ipc_request(
            {"action": "set_hotspot_uuid", "uuid": "new-uuid", "name": "New"}
        )
        self.assertEqual(resp["status"], "ok")
        daemon.monitor.invalidate.assert_called()
        self.assertEqual(self.config.designated_hotspot_uuid, "new-uuid")


class TestNotifications(DaemonHarness):
    def test_notifications_are_not_destroyed_by_reads(self):
        """BUG-14: two clients must both see every notification."""
        daemon = self.make_daemon()
        daemon._notify("Title", "Message", "normal")
        first = daemon._handle_ipc_request({"action": "get_status", "since_id": 0})
        second = daemon._handle_ipc_request({"action": "get_status", "since_id": 0})
        self.assertEqual(len(first["notifications"]), 1)
        self.assertEqual(len(second["notifications"]), 1)
        self.assertEqual(first["notifications"][0]["message"], "Message")

    def test_cursor_prevents_replay(self):
        daemon = self.make_daemon()
        daemon._notify("A", "one")
        first = daemon._handle_ipc_request({"action": "get_status", "since_id": 0})
        cursor = first["latest_notification_id"]
        second = daemon._handle_ipc_request({"action": "get_status", "since_id": cursor})
        self.assertEqual(second["notifications"], [])


class TestFirewallFailure(DaemonHarness):
    def test_apply_failure_is_surfaced(self):
        daemon = self.make_daemon()
        daemon.firewall.apply_profile.return_value = (False, "nft exploded")
        ok = daemon._apply_firewall("wlan0", reason="test")
        self.assertFalse(ok)
        self.assertEqual(daemon._firewall_error, "nft exploded")
        self.assertTrue(daemon._snapshot.get("firewall_error"))


class TestRuntimeDirectory(unittest.TestCase):
    """The daemon is a system service; clients run inside the desktop session."""

    def test_systemd_runtime_directory_is_preferred(self):
        """RuntimeDirectory= exists at boot, before /run/user/<uid> is created."""
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "data-guardian"
            runtime.mkdir(mode=0o700)
            with patch.dict(os.environ, {"RUNTIME_DIRECTORY": str(runtime)}):
                self.assertEqual(get_runtime_dir(), runtime)

    def test_owned_xdg_runtime_dir_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp, "RUNTIME_DIRECTORY": ""}):
                with patch("src.data_guardian.ipc.SYSTEM_RUNTIME_DIR", Path(tmp) / "missing"):
                    self.assertEqual(get_runtime_dir(), Path(tmp))

    def test_root_owned_runtime_dir_is_ignored(self):
        """systemd expands %U to 0 for a system unit; do not bind under /run/user/0."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_dir = root / str(os.getuid())
            user_dir.mkdir(mode=0o700)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(root / "0"),
                                         "RUNTIME_DIRECTORY": ""}):
                with patch("src.data_guardian.ipc.USER_RUNTIME_ROOT", root):
                    with patch("src.data_guardian.ipc.SYSTEM_RUNTIME_DIR", root / "missing"):
                        self.assertEqual(get_runtime_dir(), user_dir)

    def test_private_tmp_fallback_is_used_when_no_runtime_dir_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(root / "missing"),
                                         "RUNTIME_DIRECTORY": ""}):
                with patch("src.data_guardian.ipc.USER_RUNTIME_ROOT", root):
                    with patch("src.data_guardian.ipc.SYSTEM_RUNTIME_DIR", root / "missing"):
                        result = get_runtime_dir()
        self.assertEqual(result, Path("/tmp") / f"data-guardian-{os.getuid()}")
        self.assertEqual(result.stat().st_uid, os.getuid())
        self.assertEqual(result.stat().st_mode & 0o777, 0o700)


class TestIpcServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = Path(self.tmp.name) / "test.sock"
        self.patches = [patch("src.data_guardian.ipc.get_socket_path", return_value=self.sock)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_round_trip(self):
        server = IpcServer(handler=lambda req: {"status": "ok", "echo": req.get("x")})
        server.start()
        try:
            resp = send_ipc_command({"action": "ping", "x": 42})
            self.assertEqual(resp, {"status": "ok", "echo": 42})
        finally:
            server.stop()

    def test_socket_is_private(self):
        server = IpcServer(handler=lambda req: {"status": "ok"})
        server.start()
        try:
            self.assertEqual(self.sock.stat().st_mode & 0o777, 0o600)
        finally:
            server.stop()

    def test_malformed_request_returns_error(self):
        server = IpcServer(handler=lambda req: {"status": "ok"})
        server.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(self.sock))
                client.sendall(b"not json")
                data = json.loads(client.recv(4096).decode())
            self.assertEqual(data["status"], "error")
        finally:
            server.stop()

    def test_server_survives_handler_exception(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return {"status": "ok"}

        server = IpcServer(handler=handler)
        server.start()
        try:
            self.assertEqual(send_ipc_command({"action": "a"})["status"], "error")
            self.assertEqual(send_ipc_command({"action": "b"})["status"], "ok")
        finally:
            server.stop()

    def test_second_server_refuses_live_socket(self):
        """BUG-12: two daemons must not steal each other's socket."""
        first = IpcServer(handler=lambda req: {"status": "ok"})
        first.start()
        try:
            second = IpcServer(handler=lambda req: {"status": "ok"})
            with self.assertRaises(RuntimeError):
                second.start()
        finally:
            first.stop()


if __name__ == "__main__":
    unittest.main()
