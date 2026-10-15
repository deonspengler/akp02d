"""The control socket: how the editor, `akp02d ctl` and scripts talk to
a running daemon.

A Unix socket in $XDG_RUNTIME_DIR, owner-only (0600) -- only the user who
runs the daemon can reach it, and nothing on the network can. The
protocol is one JSON object per line each way, so it can be driven by
hand:

    $ echo '{"cmd": "status"}' | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/akp02d.sock
    {"ok": true, "theme": "default", ...}

Requests: {"cmd": "status"}, {"cmd": "themes"}, {"cmd": "theme", "name":
"neon", "save": false}, {"cmd": "brightness", "value": 60}, {"cmd":
"reload"}. A reply is {"ok": true, ...} or {"ok": false, "error": "..."}.

The socket is also the single-instance lock: a daemon that finds a live
one refuses to start (two would fight over the USB device), and one that
finds a dead one -- left by a crash -- removes it.

No threads. The daemon's loop used to sleep between frames; it now waits
on this socket for the same time (`serve(timeout)`), so a command is
handled within a frame and an idle loop costs what it did.
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)

# A request line longer than this is not one of ours.
MAX_LINE = 64 * 1024


class ControlError(Exception):
    """The daemon could not be reached, or said no."""


class AlreadyRunning(ControlError):
    pass


class Server:
    """The listening socket and its connections, served from the loop."""

    def __init__(self, handler: Callable[[dict], dict], path: Path | None = None) -> None:
        self.path = path or paths.socket_path()
        self._handler = handler
        if self.path.exists() or self.path.is_symlink():
            if _alive(self.path):
                raise AlreadyRunning(f"akp02d is already running (socket {self.path})")
            self.path.unlink()      # left behind by a daemon that died
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Owner-only from the moment it exists: the umask covers the gap
        # between bind and chmod.
        old = os.umask(0o177)
        try:
            listener.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        listener.listen(4)
        listener.setblocking(False)
        self._listener = listener
        self._clients: dict[socket.socket, bytes] = {}

    def serve(self, timeout: float) -> None:
        """Wait up to `timeout` seconds, handling whatever arrives.

        Returns early once a request has been answered, so the loop can
        pick up a change (a new theme) without waiting out the frame.
        """
        readers = [self._listener, *self._clients]
        try:
            ready, _, _ = select.select(readers, [], [], max(0.0, timeout))
        except InterruptedError:
            return
        for sock in ready:
            if sock is self._listener:
                self._accept()
            else:
                self._read(sock)

    def _accept(self) -> None:
        try:
            client, _ = self._listener.accept()
        except (BlockingIOError, InterruptedError):
            return
        client.setblocking(False)
        self._clients[client] = b""

    def _read(self, client: socket.socket) -> None:
        try:
            chunk = client.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._drop(client)
            return
        buffer = self._clients[client] + chunk
        *lines, rest = buffer.split(b"\n")
        if len(rest) > MAX_LINE:
            self._drop(client)
            return
        self._clients[client] = rest
        for line in lines:
            if line.strip():
                self._answer(client, line)

    def _answer(self, client: socket.socket, line: bytes) -> None:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("a request is a JSON object")
            reply = self._handler(request)
        except ValueError as exc:
            reply = {"ok": False, "error": f"not a request: {exc}"}
        except Exception as exc:  # noqa: BLE001 -- a bad command never stops the panel
            log.exception("control command failed")
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            client.setblocking(True)
            client.settimeout(1.0)
            client.sendall(json.dumps(reply).encode() + b"\n")
            client.setblocking(False)
        except OSError:
            self._drop(client)

    def _drop(self, client: socket.socket) -> None:
        self._clients.pop(client, None)
        try:
            client.close()
        except OSError:
            pass

    def close(self) -> None:
        for client in list(self._clients):
            self._drop(client)
        self._listener.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _alive(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def request(command: dict[str, Any], path: Path | None = None,
            timeout: float = 10.0) -> dict[str, Any]:
    """Send one command to the running daemon and return its reply.

    Raises ControlError if no daemon is running. A daemon that says no
    is not an exception: the reply has ok false and an error.
    """
    path = path or paths.socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise ControlError("akp02d is not running") from exc
        sock.sendall(json.dumps(command).encode() + b"\n")
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                raise ControlError("akp02d closed the connection without answering")
            buffer += chunk
    except TimeoutError as exc:
        raise ControlError("akp02d did not answer in time") from exc
    except OSError as exc:
        raise ControlError(f"cannot talk to akp02d: {exc}") from exc
    finally:
        sock.close()
    return json.loads(buffer.split(b"\n", 1)[0])
