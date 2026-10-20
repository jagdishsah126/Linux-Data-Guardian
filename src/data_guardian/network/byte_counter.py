"""Reads interface network counters from /proc/net/dev to track exact data usage."""

from __future__ import annotations
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

PROC_NET_DEV = Path("/proc/net/dev")


def format_bytes(byte_count: int) -> str:
    """Format bytes into human-readable representation (e.g., 14.5 MB)."""
    if byte_count < 1024:
        return f"{byte_count} B"
    elif byte_count < 1024 * 1024:
        return f"{byte_count / 1024:.1f} KB"
    elif byte_count < 1024 * 1024 * 1024:
        return f"{byte_count / (1024 * 1024):.1f} MB"
    else:
        return f"{byte_count / (1024 * 1024 * 1024):.2f} GB"


class InterfaceByteCounter:
    """Tracks byte usage and deltas for network interfaces using /proc/net/dev."""

    def __init__(self, interface: Optional[str] = None) -> None:
        self.target_interface = interface
        # Stores {interface: (last_rx, last_tx, last_time)}
        self._last_readings: Dict[str, Tuple[int, int, float]] = {}

    @staticmethod
    def read_all_interfaces() -> Dict[str, Tuple[int, int]]:
        """Reads /proc/net/dev and returns {interface: (rx_bytes, tx_bytes)}."""
        result: Dict[str, Tuple[int, int]] = {}
        if not PROC_NET_DEV.exists():
            return result

        try:
            with open(PROC_NET_DEV, "r", encoding="utf-8") as f:
                lines = f.readlines()

            for line in lines[2:]:  # Skip headers
                if ":" not in line:
                    continue
                iface, data = line.split(":", 1)
                iface = iface.strip()
                tokens = data.split()
                if len(tokens) >= 9:
                    rx_bytes = int(tokens[0])
                    tx_bytes = int(tokens[8])
                    result[iface] = (rx_bytes, tx_bytes)
        except Exception as e:
            print(f"[!] Error reading /proc/net/dev: {e}")

        return result

    def get_delta(self, interface: Optional[str] = None) -> Tuple[int, int, float]:
        """Calculates (delta_rx, delta_tx, speed_bytes_per_sec) since last reading.

        Returns (0, 0, 0.0) on the first sample or if interface is inactive.
        Safely handles counter resets/reconnections.
        """
        iface = interface or self.target_interface
        if not iface:
            # Fallback: pick a physical-looking interface that has traffic.
            # Virtual/container interfaces are excluded so container traffic is
            # never billed to the mobile quota.
            virtual_prefixes = ("lo", "docker", "veth", "br-", "virbr", "vmnet", "tun", "tap", "wg")
            all_ifaces = self.read_all_interfaces()
            for name, (rx, tx) in all_ifaces.items():
                if name.startswith(virtual_prefixes):
                    continue
                if rx > 0 or tx > 0:
                    iface = name
                    self.target_interface = name
                    break

        if not iface:
            return (0, 0, 0.0)

        all_readings = self.read_all_interfaces()
        if iface not in all_readings:
            return (0, 0, 0.0)

        rx_bytes, tx_bytes = all_readings[iface]
        now = time.monotonic()

        if iface not in self._last_readings:
            self._last_readings[iface] = (rx_bytes, tx_bytes, now)
            return (0, 0, 0.0)

        last_rx, last_tx, last_time = self._last_readings[iface]
        elapsed = max(now - last_time, 0.001)

        # Handle counter wrap-around or interface reconnect reset
        if rx_bytes < last_rx or tx_bytes < last_tx:
            delta_rx = rx_bytes
            delta_tx = tx_bytes
        else:
            delta_rx = rx_bytes - last_rx
            delta_tx = tx_bytes - last_tx

        self._last_readings[iface] = (rx_bytes, tx_bytes, now)
        speed = (delta_rx + delta_tx) / elapsed

        return (delta_rx, delta_tx, speed)

    def reset_interface(self, interface: str) -> None:
        """Clear cache for an interface (e.g., upon disconnect)."""
        self._last_readings.pop(interface, None)
