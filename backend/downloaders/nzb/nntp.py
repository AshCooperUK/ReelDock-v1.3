"""A small NNTP client. It downloads one article at a time over a socket."""

from __future__ import annotations

import socket
import ssl
import threading
from ...vpn.transport import nzb


class NntpError(ValueError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Reader:
    def __init__(self, sock: socket.socket, checkpoint=None) -> None:
        self.checkpoint = checkpoint
        self.sock = sock
        self.pending = b""

    def line(self) -> bytes:
        while b"\n" not in self.pending:
            if self.checkpoint is not None and not self.checkpoint():
                raise NntpError(0, "Download stopped.")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise NntpError(0, "The Usenet server closed the connection.")
            self.pending += chunk
            if len(self.pending) > 8_000_000:
                raise NntpError(0, "The Usenet server sent an oversized line.")
        raw, self.pending = self.pending.split(b"\n", 1)
        return raw[:-1] if raw.endswith(b"\r") else raw

    def dot_lines(self) -> list[bytes]:
        lines = []
        while True:
            raw = self.line()
            if raw == b".":
                return lines
            if raw.startswith(b".."):
                raw = raw[1:]
            lines.append(raw)
            if len(lines) > 200_000:
                raise NntpError(0, "That article is too large.")


class NntpClient:
    def __init__(self, host: str, port: int, use_ssl: bool, username: str, password: str, timeout: float = 45) -> None:
        self._socket_lock = threading.Lock()
        self._aborted = threading.Event()
        self.checkpoint = None
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.username = username
        self.password = password
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.reader: _Reader | None = None

    @nzb
    def connect(self) -> None:
        self.close()
        if self._aborted.is_set():
            raise NntpError(0, "Download stopped.")
        raw = socket.create_connection((self.host, self.port), self.timeout)
        raw.settimeout(self.timeout)
        if self.use_ssl:
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=self.host)
        with self._socket_lock:
            if self._aborted.is_set():
                raw.close()
                raise NntpError(0, "Download stopped.")
            self.sock = raw
            self.reader = _Reader(raw, self.checkpoint)
        code, text = self._status()
        if code not in {200, 201}:
            raise NntpError(code, auth_failure(code, text) if _busy(code, text) else (text or "The Usenet server refused the connection."))
        if self.username:
            self._authenticate()

    def abort(self) -> None:
        """Wake the owning thread without releasing a descriptor used by TLS.

        Closing an SSLSocket from another thread can recycle its descriptor
        while OpenSSL is still doing I/O. Only the I/O owner calls close().
        """
        self._aborted.set()
        with self._socket_lock:
            if self.sock is not None:
                try:
                    # Bypass SSLSocket.shutdown's mutation of the SSL object.
                    socket.socket.shutdown(self.sock, socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self) -> None:
        """Release the socket after the owning thread has left its I/O call."""
        with self._socket_lock:
            sock = self.sock
            self.sock = None
            self.reader = None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def body(self, message_id: str, groups: tuple[str, ...] = ()) -> list[bytes]:
        code, text = self._article("BODY", message_id, groups)
        if code != 222 or self.reader is None:
            raise NntpError(code, _public(text) or "That article is not on this server.")
        return self.reader.dot_lines()

    def stat(self, message_id: str, groups: tuple[str, ...] = ()) -> bool:
        """Ask whether the server has an article, without downloading it."""
        code, text = self._article("STAT", message_id, groups)
        if code == 223:
            return True
        if code in {420, 423, 430}:
            return False
        raise NntpError(code, _public(text) or "That article is not on this server.")

    def _article(self, verb: str, message_id: str, groups: tuple[str, ...]) -> tuple[int, str]:
        if self.sock is None:
            self.connect()
        identifier = message_id if message_id.startswith("<") else f"<{message_id}>"
        code, text = self._command(f"{verb} {identifier}")
        if code == 480:
            self._authenticate()
            code, text = self._command(f"{verb} {identifier}")
        if code == 412 and groups:
            group = groups[0].replace(" ", "")
            grouped, _group_text = self._command(f"GROUP {group}")
            if grouped not in {211}:
                raise NntpError(grouped, "The Usenet server does not have that group.")
            code, text = self._command(f"{verb} {identifier}")
        return code, text

    def _authenticate(self) -> None:
        code, text = self._command(f"AUTHINFO USER {self.username}")
        if code == 281:
            return
        if code != 381:
            raise NntpError(code, "The Usenet server rejected the username.")
        code, text = self._command(f"AUTHINFO PASS {self.password}")
        if code != 281:
            raise NntpError(code, auth_failure(code, text))

    def _command(self, command: str) -> tuple[int, str]:
        if self._aborted.is_set():
            raise NntpError(0, "Download stopped.")
        if self.sock is None:
            raise NntpError(0, "Not connected.")
        self.sock.sendall(command.encode("utf-8") + b"\r\n")
        return self._status()

    def _status(self) -> tuple[int, str]:
        if self.reader is None:
            raise NntpError(0, "Not connected.")
        line = self.reader.line().decode("utf-8", "replace")
        if len(line) < 3 or not line[:3].isdigit():
            raise NntpError(0, "The Usenet server sent an unexpected reply.")
        return int(line[:3]), line[4:]


def _busy(code: int, text: str) -> bool:
    lowered = (text or "").lower()
    return code == 502 or "too many" in lowered or "connection limit" in lowered


def auth_failure(code: int, text: str) -> str:
    """A full account is not a wrong password. The download can wait for a free connection."""
    if _busy(code, text):
        return "The Usenet account is using all of its connections. This download will start when one finishes."
    return "The Usenet server rejected the password."


def _public(text: str) -> str:
    lowered = text.lower()
    if "auth" in lowered or "password" in lowered:
        return "The Usenet server asked for a username and password."
    return text[:180]
