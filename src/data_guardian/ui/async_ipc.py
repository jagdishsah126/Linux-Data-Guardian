"""Non-blocking IPC helper for GTK clients.

Calling ``send_ipc_command`` on the GTK main thread freezes the UI whenever the
daemon is busy (it performs privileged firewall work). All UI call sites go
through this helper instead.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from gi.repository import GLib

from src.data_guardian.ipc import send_ipc_command


def ipc_async(
    request: Dict[str, Any],
    callback: Optional[Callable[[Optional[Dict[str, Any]]], None]] = None,
    timeout: float = 3.0,
) -> None:
    """Run an IPC request in a worker thread and deliver the result on the main loop."""

    def worker() -> None:
        response = send_ipc_command(request, timeout=timeout)
        if callback is not None:
            GLib.idle_add(callback, response)

    threading.Thread(target=worker, daemon=True, name="dg-ipc-call").start()
