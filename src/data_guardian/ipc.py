"""UNIX domain socket IPC between the Data Guardian daemon and UI clients."""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 262144
PROTOCOL_VERSION = 2


def get_runtime_dir() -> Path:
    """Runtime directory for sockets and lock files (0700)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime)
    fallback = Path("/tmp") / f"data-guardian-{os.getuid()}"
    fallback.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Refuse a pre-created directory owned by somebody else (symlink/ownership
    # attack on a shared /tmp).
    if fallback.stat().st_uid != os.getuid():
        raise RuntimeError(f"runtime directory {fallback} is not owned by this user")
    try:
        os.chmod(fallback, 0o700)
    except OSError:
        pass
    return fallback


def get_socket_path() -> Path:
    return get_runtime_dir() / "data-guardian.sock"


class IpcServer:
    """Lightweight UNIX domain socket server for daemon control."""

    def __init__(self, handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        self.socket_path = get_socket_path()
        self.handler = handler
        self._server_socket: Optional[socket.socket] = None
        self._is_running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Publish the socket atomically so clients never see a half-open socket."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove only a *stale* socket; if a live peer answers, do not steal it.
        if self.socket_path.exists():
            if self._peer_is_alive():
                raise RuntimeError(f"another daemon is already listening on {self.socket_path}")
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        tmp_path = self.socket_path.with_suffix(f".sock.{os.getpid()}")
        try:
            tmp_path.unlink()
        except OSError:
            pass

        old_umask = os.umask(0o077)
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(tmp_path))
            os.chmod(tmp_path, 0o600)
            server.listen(8)
            os.replace(tmp_path, self.socket_path)
        finally:
            os.umask(old_umask)

        self._server_socket = server
        self._is_running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="dg-ipc")
        self._thread.start()

    def _peer_is_alive(self) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                probe.connect(str(self.socket_path))
                probe.sendall(b'{"action":"ping"}')
                return bool(probe.recv(1024))
        except OSError:
            return False

    def _listen_loop(self) -> None:
        while self._is_running and self._server_socket is not None:
            try:
                conn, _ = self._server_socket.accept()
            except OSError:
                if self._is_running:
                    continue
                break
            except Exception:  # noqa: BLE001 - never let the listener die silently
                if self._is_running:
                    continue
                break
            threading.Thread(
                target=self._handle_client, args=(conn,), daemon=True, name="dg-ipc-client"
            ).start()

    def _handle_client(self, conn: socket.socket) -> None:
        with conn:
            try:
                data = conn.recv(MAX_REQUEST_BYTES)
                if not data:
                    return
                req = json.loads(data.decode("utf-8"))
                if not isinstance(req, dict):
                    raise ValueError("request must be a JSON object")
                response = self.handler(req)
                resp_bytes = json.dumps(response).encode("utf-8")[:MAX_RESPONSE_BYTES]
            except Exception as exc:  # noqa: BLE001
                resp_bytes = json.dumps({"status": "error", "message": str(exc)}).encode("utf-8")
            try:
                conn.sendall(resp_bytes)
            except OSError:
                pass

    def stop(self) -> None:
        self._is_running = False
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except OSError:
            pass


def send_ipc_command(
    request: Dict[str, Any], timeout: float = 3.0
) -> Optional[Dict[str, Any]]:
    """Send a JSON request to the running daemon and return the parsed response."""
    sock_path = get_socket_path()
    if not sock_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(sock_path))
            sock.sendall(json.dumps(request).encode("utf-8"))
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if len(b"".join(chunks)) > MAX_RESPONSE_BYTES:
                    break
            data = b"".join(chunks)
            if data:
                return json.loads(data.decode("utf-8"))
    except Exception:  # noqa: BLE001 - callers treat failures as "daemon unavailable"
        return None
    return None
