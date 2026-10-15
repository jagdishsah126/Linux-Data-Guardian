"""Unit tests for NetworkManager monitor."""

import unittest
from src.data_guardian.network.nm_monitor import NetworkMonitor, ConnectionInfo


class TestNetworkMonitor(unittest.TestCase):
    def test_active_connection_detection(self):
        monitor = NetworkMonitor()
        active = monitor.get_active_connection()
        # Since this machine is currently connected to Wi-Fi, active connection should not be None
        self.assertIsNotNone(active)
        self.assertIsInstance(active, ConnectionInfo)
        self.assertTrue(len(active.uuid) > 0)
        self.assertTrue(len(active.name) > 0)

    def test_saved_connections_list(self):
        saved = NetworkMonitor.list_saved_connections()
        self.assertIsInstance(saved, list)
        self.assertGreater(len(saved), 0)
        # Check keys
        self.assertIn("uuid", saved[0])
        self.assertIn("name", saved[0])

    def test_designated_hotspot_matching(self):
        monitor = NetworkMonitor()
        active = monitor.get_active_connection()
        self.assertIsNotNone(active)

        # Match by active UUID
        monitor.designated_uuid = active.uuid
        self.assertTrue(monitor.is_designated_hotspot_active())

        # Non-matching UUID
        monitor.designated_uuid = "non-existent-uuid-0000"
        self.assertFalse(monitor.is_designated_hotspot_active())

    def test_transition_callbacks(self):
        active = NetworkMonitor().get_active_connection()
        self.assertIsNotNone(active)

        events = []
        monitor = NetworkMonitor(designated_uuid=active.uuid)

        # First poll: was not active, now active -> triggers on_hotspot_activated
        is_active = monitor.poll_check(
            on_hotspot_activated=lambda conn: events.append("activated"),
            on_hotspot_deactivated=lambda: events.append("deactivated")
        )
        self.assertTrue(is_active)
        self.assertEqual(events, ["activated"])

        # Change designated to different UUID -> triggers deactivated
        monitor.designated_uuid = "other-uuid"
        is_active2 = monitor.poll_check(
            on_hotspot_activated=lambda conn: events.append("activated_again"),
            on_hotspot_deactivated=lambda: events.append("deactivated")
        )
        self.assertFalse(is_active2)
        self.assertEqual(events, ["activated", "deactivated"])


if __name__ == "__main__":
    unittest.main()
