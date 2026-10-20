"""Unit tests for UI synchronization and initial selection in Dashboard and Tray."""

import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile

from src.data_guardian.config import ConfigManager
from src.data_guardian.database import Database


class TestUISync(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp_dir.name) / "config.yaml"
        self.db_path = Path(self.tmp_dir.name) / "guardian.db"
        self.config_mgr = ConfigManager(config_path=self.config_path)
        self.config_mgr.config["storage"] = {"db_path": str(self.db_path)}
        self.config_mgr.config["hotspot"] = {
            "designated_uuid": "test-hotspot-uuid-1234",
            "designated_name": "Test Hotspot",
        }
        self.config_mgr.save_user_config()
        self.db = Database(self.db_path)

        # Pre-populate some network usage
        self.db.record_usage_delta(
            delta_rx=10 * 1024 * 1024,
            delta_tx=5 * 1024 * 1024,
            connection_uuid="test-hotspot-uuid-1234",
            connection_name="Test Hotspot"
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_network_profile_persistence_on_daemon_set_profile(self):
        from src.data_guardian.daemon import DataGuardianDaemon
        with patch("src.data_guardian.daemon.NftablesManager"), \
             patch("src.data_guardian.daemon.NetworkMonitor") as mock_nm:
            
            mock_conn = MagicMock()
            mock_conn.uuid = "test-hotspot-uuid-1234"
            mock_conn.name = "Test Hotspot"
            mock_conn.interface = "wlan0"
            mock_nm.return_value.get_active_connection.return_value = mock_conn
            mock_nm.return_value.is_designated_hotspot_active.return_value = True

            daemon = DataGuardianDaemon(config_manager=self.config_mgr)
            resp = daemon._handle_ipc_request({"action": "set_profile", "profile": "strict"})

            self.assertEqual(resp.get("status"), "ok")
            self.assertEqual(resp.get("profile"), "strict")
            self.assertEqual(daemon._current_profile, "strict")

            # Check that network_profile was persisted in config
            saved_prof = self.config_mgr.get_network_profile("test-hotspot-uuid-1234")
            self.assertEqual(saved_prof, "strict")

    def test_tray_loading_profile_guard(self):
        from src.data_guardian.ui.tray import DataGuardianTray
        with patch.object(DataGuardianTray, "_init_xapp", lambda self: None), \
             patch.object(DataGuardianTray, "_init_appindicator", lambda self: None), \
             patch.object(DataGuardianTray, "_init_gtk_status_icon", lambda self: None), \
             patch.object(DataGuardianTray, "_refresh_tray", lambda self: None), \
             patch("src.data_guardian.ui.tray.ipc_async") as mock_ipc:
            
            tray = DataGuardianTray()
            self.assertFalse(tray._loading_profile)

            # When loading_profile is True, callback should return without sending IPC
            tray._loading_profile = True
            tray._on_profile_selected("strict", True)
            mock_ipc.assert_not_called()

            # Inactive toggles (radio group updates) must not send either.
            tray._loading_profile = False
            tray._on_profile_selected("strict", False)
            mock_ipc.assert_not_called()

            # When loading_profile is False, the request goes out off the UI thread.
            tray._on_profile_selected("strict", True)
            mock_ipc.assert_called_once()
            self.assertEqual(
                mock_ipc.call_args.args[0],
                {"action": "set_profile", "profile": "strict"},
            )


if __name__ == "__main__":
    unittest.main()
