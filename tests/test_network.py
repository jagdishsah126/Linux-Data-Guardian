"""NetworkManager parsing and byte counter (regression tests for BUG-30/44)."""

import unittest
from unittest.mock import patch

from src.data_guardian.network.byte_counter import InterfaceByteCounter, format_bytes
from src.data_guardian.network.nm_monitor import ConnectionInfo, split_nmcli_line


class TestNmcliParsing(unittest.TestCase):
    def test_plain_line(self):
        self.assertEqual(split_nmcli_line("uuid:Name:802-11-wireless:wlan0"),
                         ["uuid", "Name", "802-11-wireless", "wlan0"])

    def test_ssid_with_colon(self):
        """BUG-30: an SSID containing ':' must not shift the fields."""
        line = r"uuid-1:Example Wi-Fi\: 5G:802-11-wireless:wlan0"
        self.assertEqual(split_nmcli_line(line),
                         ["uuid-1", "Example Wi-Fi: 5G", "802-11-wireless", "wlan0"])

    def test_escaped_backslash(self):
        line = r"uuid:Name\\:802-3-ethernet:eth0"
        self.assertEqual(split_nmcli_line(line),
                         ["uuid", "Name\\", "802-3-ethernet", "eth0"])

    def test_empty_fields(self):
        self.assertEqual(split_nmcli_line("a::c"), ["a", "", "c"])


class TestConnectionMatching(unittest.TestCase):
    def test_matches_by_uuid_then_name(self):
        from src.data_guardian.network.nm_monitor import NetworkMonitor

        conn = ConnectionInfo("u1", "Example Wi-Fi", "802-11-wireless", "wlan0", True)
        monitor = NetworkMonitor(designated_uuid="u1")
        self.assertTrue(monitor.matches_designated(conn))
        monitor = NetworkMonitor(designated_uuid="", designated_name="example wi-fi")
        self.assertTrue(monitor.matches_designated(conn))
        monitor = NetworkMonitor(designated_uuid="other")
        self.assertFalse(monitor.matches_designated(conn))

    def test_no_designation_never_matches(self):
        from src.data_guardian.network.nm_monitor import NetworkMonitor

        conn = ConnectionInfo("u1", "Any", "802-11-wireless", "wlan0", False)
        self.assertFalse(NetworkMonitor().matches_designated(conn))

    def test_transitions_fire_once(self):
        from src.data_guardian.network.nm_monitor import NetworkMonitor

        conn = ConnectionInfo("u1", "Hot", "802-11-wireless", "wlan0", True)
        monitor = NetworkMonitor(designated_uuid="u1")
        activated, deactivated = [], []
        with patch.object(monitor, "get_active_connection", return_value=conn):
            for _ in range(3):
                monitor.poll_check(lambda c: activated.append(c), lambda: deactivated.append(1))
        self.assertEqual(len(activated), 1)
        self.assertEqual(deactivated, [])

        with patch.object(monitor, "get_active_connection", return_value=None):
            monitor.poll_check(lambda c: activated.append(c), lambda: deactivated.append(1))
        self.assertEqual(len(deactivated), 1)


class TestByteCounter(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(format_bytes(500), "500 B")
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(15 * 1024 * 1024), "15.0 MB")
        self.assertEqual(format_bytes(2 * 1024 * 1024 * 1024), "2.00 GB")

    def test_reads_loopback(self):
        interfaces = InterfaceByteCounter.read_all_interfaces()
        self.assertIn("lo", interfaces)

    def test_delta_from_synthetic_readings(self):
        counter = InterfaceByteCounter(interface="wlan0")
        readings = {"wlan0": (1000, 500)}
        with patch.object(InterfaceByteCounter, "read_all_interfaces", return_value=readings):
            self.assertEqual(counter.get_delta("wlan0"), (0, 0, 0.0))
            readings["wlan0"] = (3000, 1500)
            rx, tx, speed = counter.get_delta("wlan0")
        self.assertEqual((rx, tx), (2000, 1000))
        self.assertGreaterEqual(speed, 0)

    def test_counter_reset_is_handled(self):
        counter = InterfaceByteCounter(interface="wlan0")
        readings = {"wlan0": (10_000, 10_000)}
        with patch.object(InterfaceByteCounter, "read_all_interfaces", return_value=readings):
            counter.get_delta("wlan0")
            readings["wlan0"] = (100, 50)  # interface reconnected
            rx, tx, _ = counter.get_delta("wlan0")
        self.assertEqual((rx, tx), (100, 50))

    def test_virtual_interfaces_are_not_auto_selected(self):
        counter = InterfaceByteCounter()
        readings = {"docker0": (10**9, 10**9), "wlan0": (5, 5)}
        with patch.object(InterfaceByteCounter, "read_all_interfaces", return_value=readings):
            counter.get_delta()
        self.assertEqual(counter.target_interface, "wlan0")


if __name__ == "__main__":
    unittest.main()
