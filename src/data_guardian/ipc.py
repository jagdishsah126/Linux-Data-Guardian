"""UNIX domain socket IPC between Data Guardian daemon and UI clients."""

from __future__ import annotations
import os
import json
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

def get_socket_path() -> Path:
    """Returns runtime socket path for current user."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and Path(runtime_dir).exists():
        return Path(runtime_dir) / "data-guardian.sock"
    return Path(f"/tmp/data-guardian-{os.getuid()}.sock")


class IpcServer:
    """Lightweight UNIX domain socket server for daemon control."""

    def __init__(self, handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        self.socket_path = get_socket_path()
        self.handler = handler
        self._server_socket: Optional[socket.socket] = None
        self._is_running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Starts socket listener in a background thread."""
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass

        self._server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_socket.bind(str(self.socket_path))
        self._server_socket.listen(5)
        # Ensure user permissions
        self.socket_path.chmod(0o600)

        self._is_running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def _listen_loop(self) -> None:
        while self._is_running and self._server_socket:
            try:
                conn, _ = self._server_socket.accept()
                threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
            except Exception:
                break

    def _handle_client(self, conn: socket.socket) -> None:
        with conn:
            try:
                data = conn.recv(8192)
                if not data:
                    return
                req = json.loads(data.decode("utf-8"))
                response = self.handler(req)
                resp_bytes = json.dumps(response).encode("utf-8")
                conn.sendall(resp_bytes)
            except Exception as e:
                err_resp = json.dumps({"status": "error", "message": str(e)}).encode("utf-8")
                conn.sendall(err_resp)

    def stop(self) -> None:
        """Stops the socket server and removes the socket file."""
        self._is_running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass
            self._server_socket = None

        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass


def send_ipc_command(request: Dict[str, Any], timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    """Sends a JSON request to the running daemon and returns the response."""
    sock_path = get_socket_path()
    if not sock_path.exists():
        return None

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(sock_path))
            sock.sendall(json.dumps(request).encode("utf-8"))
            data = sock.recv(8192)
            if data:
                return json.loads(data.decode("utf-8"))
    except Exception:
        return None
    return None
