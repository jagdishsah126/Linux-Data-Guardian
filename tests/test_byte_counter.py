"""Unit tests for /proc/net/dev byte counter and formatting."""

import unittest
from src.data_guardian.network.byte_counter import InterfaceByteCounter, format_bytes


class TestByteCounter(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(format_bytes(500), "500 B")
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(15 * 1024 * 1024), "15.0 MB")
        self.assertEqual(format_bytes(2 * 1024 * 1024 * 1024), "2.00 GB")

    def test_read_real_interfaces(self):
        counter = InterfaceByteCounter()
        interfaces = counter.read_all_interfaces()
        self.assertIsInstance(interfaces, dict)
        # 'lo' should always exist on Linux
        self.assertIn("lo", interfaces)
        rx, tx = interfaces["lo"]
        self.assertGreaterEqual(rx, 0)
        self.assertGreaterEqual(tx, 0)

    def test_delta_calculation(self):
        counter = InterfaceByteCounter(interface="lo")
        # First reading returns (0, 0, 0.0)
        delta_rx, delta_tx, speed = counter.get_delta("lo")
        self.assertEqual(delta_rx, 0)
        self.assertEqual(delta_tx, 0)


if __name__ == "__main__":
    unittest.main()
