"""NetworkManager monitor for detecting metered hotspot connections."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

HAS_LIBNM = False
try:  # pragma: no cover - depends on the host
    import gi

    gi.require_version("NM", "1.0")
    from gi.repository import NM

    HAS_LIBNM = True
except Exception:  # noqa: BLE001
    HAS_LIBNM = False


@dataclass
class ConnectionInfo:
    uuid: str
    name: str
    conn_type: str
    interface: str
    is_metered: bool


def split_nmcli_line(line: str) -> List[str]:
    """Split an ``nmcli -t`` line on unescaped colons.

    nmcli escapes embedded colons (and backslashes) with a backslash, so a naive
    ``split(':')`` corrupts SSIDs such as ``Good Day: 5G``.
    """
    fields: List[str] = []
    current: List[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def default_route_interface() -> str:
    """Interface carrying the IPv4 default route (best-effort)."""
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as handle:
            for line in handle.readlines()[1:]:
                fields = line.split()
                if len(fields) > 3 and fields[1] == "00000000":
                    return fields[0]
    except OSError:
        pass
    return ""


class NetworkMonitor:
    """Monitors NetworkManager connection state and matches the designated hotspot."""

    def __init__(self, designated_uuid: str = "", designated_name: str = "") -> None:
        self.designated_uuid = designated_uuid
        self.designated_name = designated_name
        self.current_connection: Optional[ConnectionInfo] = None
        self._nm_client = None
        self._is_hotspot_active = False

        if HAS_LIBNM:
            try:
                self._nm_client = NM.Client.new(None)
            except Exception as exc:  # noqa: BLE001
                print(f"[!] Warning: Failed to initialize NM.Client: {exc}")
                self._nm_client = None

    def invalidate(self) -> None:
        """Force the next poll to re-evaluate transitions."""
        self._is_hotspot_active = False
        self.current_connection = None

    # ------------------------------------------------------------------ queries

    @staticmethod
    def _candidate_is_primary(candidate: ConnectionInfo, primary_iface: str) -> int:
        """Lower is better: prefer the default-route interface."""
        if primary_iface and candidate.interface == primary_iface:
            return 0
        return 1

    def get_active_connection(self) -> Optional[ConnectionInfo]:
        """Primary active Wi-Fi/Ethernet connection, preferring the default route."""
        candidates: List[ConnectionInfo] = []

        if self._nm_client:
            try:
                for ac in self._nm_client.get_active_connections():
                    conn_type = ac.get_connection_type()
                    if conn_type not in ("802-11-wireless", "802-3-ethernet"):
                        continue
                    devices = ac.get_devices()
                    iface = devices[0].get_iface() if devices else ""
                    is_metered = False
                    if devices and hasattr(devices[0], "get_metered"):
                        # NM.Metered: UNKNOWN=0, YES=1, NO=2, GUESS_YES=3, GUESS_NO=4
                        is_metered = devices[0].get_metered() in (1, 3)
                    candidates.append(ConnectionInfo(
                        uuid=ac.get_uuid() or "",
                        name=ac.get_id() or "",
                        conn_type=conn_type,
                        interface=iface,
                        is_metered=is_metered,
                    ))
            except Exception as exc:  # noqa: BLE001
                print(f"[!] Warning: Error querying libnm: {exc}")

        if not candidates:
            candidates = self._active_connections_nmcli()

        if not candidates:
            return None

        primary = default_route_interface()
        return sorted(candidates, key=lambda c: self._candidate_is_primary(c, primary))[0]

    @staticmethod
    def _active_connections_nmcli() -> List[ConnectionInfo]:
        result: List[ConnectionInfo] = []
        try:
            cmd = ["nmcli", "-t", "-f", "UUID,NAME,TYPE,DEVICE", "con", "show", "--active"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, timeout=3, check=False)
            for line in res.stdout.strip().splitlines():
                parts = split_nmcli_line(line)
                if len(parts) >= 4 and parts[2] in ("802-11-wireless", "802-3-ethernet"):
                    result.append(ConnectionInfo(
                        uuid=parts[0], name=parts[1], conn_type=parts[2],
                        interface=parts[3], is_metered=False,
                    ))
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Error in nmcli query: {exc}")
        return result

    @staticmethod
    def list_saved_connections() -> List[Dict[str, str]]:
        connections: List[Dict[str, str]] = []
        try:
            cmd = ["nmcli", "-t", "-f", "UUID,NAME,TYPE", "con", "show"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, timeout=3, check=False)
            for line in res.stdout.strip().splitlines():
                parts = split_nmcli_line(line)
                if len(parts) >= 3:
                    connections.append({"uuid": parts[0], "name": parts[1], "type": parts[2]})
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Error listing connections: {exc}")
        return connections

    def matches_designated(self, active: Optional[ConnectionInfo]) -> bool:
        if not active:
            return False
        if self.designated_uuid:
            return active.uuid == self.designated_uuid
        if self.designated_name:
            return active.name.lower() == self.designated_name.lower()
        return False

    def is_designated_hotspot_active(self) -> bool:
        return self.matches_designated(self.get_active_connection())

    # ------------------------------------------------------------------- polling

    def poll_check(
        self,
        on_hotspot_activated: Optional[Callable[[ConnectionInfo], None]] = None,
        on_hotspot_deactivated: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Fire transition callbacks; return True while the hotspot is active."""
        active = self.get_active_connection()
        is_now_hotspot = self.matches_designated(active)

        if is_now_hotspot and not self._is_hotspot_active:
            self._is_hotspot_active = True
            if on_hotspot_activated and active:
                on_hotspot_activated(active)
        elif not is_now_hotspot and self._is_hotspot_active:
            self._is_hotspot_active = False
            if on_hotspot_deactivated:
                on_hotspot_deactivated()

        self.current_connection = active
        return is_now_hotspot
