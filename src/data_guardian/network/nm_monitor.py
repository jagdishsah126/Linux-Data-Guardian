"""NetworkManager monitor for detecting metered hotspot connections."""

from __future__ import annotations
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

# Attempt to use native libnm via PyGObject if available
HAS_LIBNM = False
try:
    import gi
    gi.require_version("NM", "1.0")
    from gi.repository import NM
    HAS_LIBNM = True
except Exception:
    HAS_LIBNM = False


@dataclass
class ConnectionInfo:
    uuid: str
    name: str
    conn_type: str
    interface: str
    is_metered: bool


class NetworkMonitor:
    """Monitors NetworkManager connection state and matches designated hotspot."""

    def __init__(self, designated_uuid: str = "", designated_name: str = "") -> None:
        self.designated_uuid = designated_uuid
        self.designated_name = designated_name
        self.current_connection: Optional[ConnectionInfo] = None
        self._nm_client = None
        self._is_hotspot_active: bool = False

        if HAS_LIBNM:
            try:
                self._nm_client = NM.Client.new(None)
            except Exception as e:
                print(f"[!] Warning: Failed to initialize NM.Client: {e}")
                self._nm_client = None

    def get_active_connection(self) -> Optional[ConnectionInfo]:
        """Returns the primary active Wi-Fi or Ethernet connection."""
        if self._nm_client:
            try:
                for ac in self._nm_client.get_active_connections():
                    conn_type = ac.get_connection_type()
                    if conn_type in ("802-11-wireless", "802-3-ethernet"):
                        devices = ac.get_devices()
                        iface = devices[0].get_iface() if devices else ""
                        uuid = ac.get_uuid() or ""
                        name = ac.get_id() or ""
                        is_metered = False
                        if devices and hasattr(devices[0], "get_metered"):
                            metered_val = devices[0].get_metered()
                            # NM.Metered: YES = 1, NO = 2, GUESS_YES = 3, GUESS_NO = 4
                            is_metered = metered_val in (1, 3)
                        return ConnectionInfo(
                            uuid=uuid,
                            name=name,
                            conn_type=conn_type,
                            interface=iface,
                            is_metered=is_metered,
                        )
            except Exception as e:
                print(f"[!] Warning: Error querying libnm: {e}")

        # Fallback to nmcli
        return self._get_active_connection_nmcli()

    @staticmethod
    def _get_active_connection_nmcli() -> Optional[ConnectionInfo]:
        """Fallback querying active connection via nmcli."""
        try:
            cmd = ["nmcli", "-t", "-f", "UUID,NAME,TYPE,DEVICE", "con", "show", "--active"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3)
            for line in res.stdout.strip().splitlines():
                parts = line.split(":")
                if len(parts) >= 4:
                    uuid, name, conn_type, device = parts[0], parts[1], parts[2], parts[3]
                    if conn_type in ("802-11-wireless", "802-3-ethernet"):
                        return ConnectionInfo(
                            uuid=uuid,
                            name=name,
                            conn_type=conn_type,
                            interface=device,
                            is_metered=False,
                        )
        except Exception as e:
            print(f"[!] Error in nmcli query: {e}")
        return None

    @staticmethod
    def list_saved_connections() -> List[Dict[str, str]]:
        """Lists all known saved connections on the system."""
        connections: List[Dict[str, str]] = []
        try:
            cmd = ["nmcli", "-t", "-f", "UUID,NAME,TYPE", "con", "show"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3)
            for line in res.stdout.strip().splitlines():
                parts = line.split(":")
                if len(parts) >= 3:
                    connections.append({
                        "uuid": parts[0],
                        "name": parts[1],
                        "type": parts[2],
                    })
        except Exception as e:
            print(f"[!] Error listing connections: {e}")
        return connections

    def is_designated_hotspot_active(self) -> bool:
        """Checks if the currently active connection matches the designated hotspot."""
        active = self.get_active_connection()
        if not active:
            return False

        if self.designated_uuid:
            return active.uuid == self.designated_uuid

        if self.designated_name:
            return active.name.lower() == self.designated_name.lower()

        return False

    def poll_check(
        self,
        on_hotspot_activated: Optional[Callable[[ConnectionInfo], None]] = None,
        on_hotspot_deactivated: Optional[Callable[[], None]] = None
    ) -> bool:
        """Checks connection status and triggers callbacks on transition.

        Returns True if designated hotspot is currently active.
        """
        active = self.get_active_connection()
        is_now_hotspot = False
        if active:
            if self.designated_uuid:
                is_now_hotspot = (active.uuid == self.designated_uuid)
            elif self.designated_name:
                is_now_hotspot = (active.name.lower() == self.designated_name.lower())

        # Transition into designated hotspot
        if is_now_hotspot and not self._is_hotspot_active:
            self._is_hotspot_active = True
            if on_hotspot_activated and active:
                on_hotspot_activated(active)

        # Transition out of designated hotspot
        elif not is_now_hotspot and self._is_hotspot_active:
            self._is_hotspot_active = False
            if on_hotspot_deactivated:
                on_hotspot_deactivated()

        self.current_connection = active
        return is_now_hotspot
