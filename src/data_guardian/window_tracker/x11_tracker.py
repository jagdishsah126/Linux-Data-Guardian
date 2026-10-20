"""X11 active window detection for XFCE desktop session."""

from __future__ import annotations
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set


@dataclass
class ActiveWindowInfo:
    window_id: str
    pid: Optional[int]
    app_name: str
    wm_class: str
    title: str


class X11WindowTracker:
    """Tracks active foreground window in XFCE / X11 session."""

    def __init__(self) -> None:
        self.last_window_id: Optional[str] = None
        self.last_app_name: Optional[str] = None

    @staticmethod
    def is_x11_available() -> bool:
        """Check if an X11 display is currently accessible."""
        display = os.environ.get("DISPLAY")
        if not display:
            return False
        return Path("/usr/bin/xprop").exists()

    @staticmethod
    def autodetect_environment() -> bool:
        """Best-effort discovery of DISPLAY/XAUTHORITY for a systemd service.

        A system service inherits no session environment, so ``xprop`` fails and
        the foreground app is always ``unknown``. We ask logind instead.
        """
        if os.environ.get("DISPLAY"):
            return True

        try:
            import pwd

            uid = os.getuid()
            username = pwd.getpwuid(uid).pw_name
            home = pwd.getpwuid(uid).pw_dir
        except Exception:  # noqa: BLE001
            return False

        display = ""
        try:
            sessions = subprocess.run(
                ["loginctl", "list-sessions", "--no-legend", "--no-pager"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3,
            ).stdout
            for line in sessions.splitlines():
                fields = line.split()
                if not fields:
                    continue
                session_id = fields[0]
                prop = subprocess.run(
                    ["loginctl", "show-session", session_id, "-p", "Name", "-p", "Type", "-p", "Display"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3,
                ).stdout
                info = dict(
                    line.split("=", 1) for line in prop.splitlines() if "=" in line
                )
                if info.get("Name") == username and info.get("Type") in ("x11", ""):
                    display = info.get("Display", "") or display
                    if display:
                        break
        except Exception:  # noqa: BLE001
            pass

        if not display:
            return False

        os.environ["DISPLAY"] = display
        xauthority = Path(home) / ".Xauthority"
        if xauthority.exists() and not os.environ.get("XAUTHORITY"):
            os.environ["XAUTHORITY"] = str(xauthority)
        return True

    def get_active_window(self) -> Optional[ActiveWindowInfo]:
        """Queries X11 root window to determine which application has focus."""
        if not self.is_x11_available():
            return None

        try:
            # 1. Get active window ID
            root_cmd = ["/usr/bin/xprop", "-root", "_NET_ACTIVE_WINDOW"]
            res = subprocess.run(root_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1)
            if res.returncode != 0 or "_NET_ACTIVE_WINDOW" not in res.stdout:
                return None

            line = res.stdout.strip()
            # Example: _NET_ACTIVE_WINDOW(WINDOW): window id # 0x3000004
            parts = line.split("#", 1)
            if len(parts) < 2:
                parts = line.split("window id", 1)
                if len(parts) < 2:
                    return None

            win_id_str = parts[1].strip().split(",")[0].strip()
            if not win_id_str or win_id_str == "0x0":
                return None

            # 2. Get window properties (PID, WM_CLASS, WM_NAME)
            prop_cmd = ["/usr/bin/xprop", "-id", win_id_str, "_NET_WM_PID", "WM_CLASS", "WM_NAME"]
            prop_res = subprocess.run(prop_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1)

            pid: Optional[int] = None
            wm_class = ""
            title = ""

            for p_line in prop_res.stdout.splitlines():
                p_line = p_line.strip()
                if p_line.startswith("_NET_WM_PID"):
                    val = p_line.split("=", 1)[-1].strip()
                    if val.isdigit():
                        pid = int(val)
                elif p_line.startswith("WM_CLASS"):
                    val = p_line.split("=", 1)[-1].strip()
                    wm_class = val.replace('"', '')
                elif p_line.startswith("WM_NAME"):
                    title = p_line.split("=", 1)[-1].strip().replace('"', '')

            # Resolve executable name from PID
            app_name = ""
            if pid:
                comm_path = Path(f"/proc/{pid}/comm")
                if comm_path.exists():
                    try:
                        app_name = comm_path.read_text(encoding="utf-8").strip()
                    except Exception:
                        pass

            if not app_name and wm_class:
                # Fallback to class name
                app_name = wm_class.split(",")[0].strip().lower()

            return ActiveWindowInfo(
                window_id=win_id_str,
                pid=pid,
                app_name=app_name.lower(),
                wm_class=wm_class,
                title=title,
            )
        except Exception as e:
            print(f"[!] Error inspecting active window: {e}")
            return None

    @staticmethod
    def get_process_family_pids(parent_pid: int) -> Set[int]:
        """Finds all child and descendant process IDs for the given parent PID."""
        pids: Set[int] = {parent_pid}
        try:
            cmd = ["/usr/bin/pgrep", "-P", str(parent_pid)]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1)
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    child_pid = int(line)
                    pids.add(child_pid)
                    # Recursive lookup for child processes
                    pids.update(X11WindowTracker.get_process_family_pids(child_pid))
        except Exception:
            pass
        return pids
