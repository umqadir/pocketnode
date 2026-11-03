"""Framing and connection handling shared by the Mac and the worker.

A frame is a small JSON header plus zero or more raw buffers:

    magic(4) | header_len(u32) | nbuf(u32) | nbuf * buf_len(u64) | header | buffers...

The header is JSON so nothing is unpickled before the peer has authenticated.
Buffers are sent with scatter-gather I/O and received straight into fresh
bytearrays, so large arrays are never copied on the way through.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import struct
import threading
from typing import Callable, Sequence

MAGIC = b"PN01"
_PREFIX = struct.Struct("!4sII")
_IOV_MAX = 512
_CHUNK = 8 << 20


class ConnectionClosed(ConnectionError):
    pass


def digest(buf) -> str:
    """Content address of a buffer (hardware-accelerated SHA-256 on Apple chips)."""
    return hashlib.sha256(buf).hexdigest()[:32]


def tune(sock: socket.socket) -> None:
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, 4 << 20)
        except OSError:
            pass


def _sendall_vec(sock: socket.socket, parts: list[memoryview]) -> None:
    # sendmsg may send part of an iovec list; walk forward until all is out.
    parts = [p for p in parts if p.nbytes]
    while parts:
        batch = parts[:_IOV_MAX]
        sent = sock.sendmsg(batch)
        while sent:
            head = parts[0]
            if sent >= head.nbytes:
                sent -= head.nbytes
                parts.pop(0)
            else:
                parts[0] = head[sent:]
                sent = 0


def _recv_exact_into(sock: socket.socket, view: memoryview) -> None:
    got = 0
    n = view.nbytes
    while got < n:
        k = sock.recv_into(view[got:], min(n - got, _CHUNK))
        if k == 0:
            raise ConnectionClosed("peer closed the connection")
        got += k


def _recv_exact(sock: socket.socket, n: int) -> bytearray:
    buf = bytearray(n)
    if n:
        _recv_exact_into(sock, memoryview(buf))
    return buf


def as_bytes_view(b) -> memoryview:
    m = b.raw() if hasattr(b, "raw") else memoryview(b)
    return m.cast("B") if m.format != "B" or m.ndim != 1 else m


class Connection:
    """A framed, thread-safe connection. One reader thread calls recv()."""

    def __init__(self, sock: socket.socket, name: str = "peer"):
        tune(sock)
        self.sock = sock
        self.name = name
        self._wlock = threading.Lock()
        self.closed = False
        self.bytes_out = 0
        self.bytes_in = 0

    def send(self, header: dict, buffers: Sequence = ()) -> None:
        views = [as_bytes_view(b) for b in buffers]
        h = json.dumps(header, separators=(",", ":")).encode()
        prefix = _PREFIX.pack(MAGIC, len(h), len(views))
        lens = struct.pack(f"!{len(views)}Q", *(v.nbytes for v in views))
        with self._wlock:
            if self.closed:
                raise ConnectionClosed(f"connection to {self.name} is closed")
            try:
                _sendall_vec(self.sock, [memoryview(prefix + lens + h), *views])
            except OSError as e:
                self.close()
                raise ConnectionClosed(str(e)) from e
            self.bytes_out += len(h) + sum(v.nbytes for v in views)

    def recv(self) -> tuple[dict, list[bytearray]]:
        try:
            magic, hlen, nbuf = _PREFIX.unpack(_recv_exact(self.sock, _PREFIX.size))
            if magic != MAGIC:
                raise ConnectionClosed(f"bad magic from {self.name}: {magic!r}")
            lens = struct.unpack(f"!{nbuf}Q", _recv_exact(self.sock, 8 * nbuf)) if nbuf else ()
            header = json.loads(_recv_exact(self.sock, hlen))
            bufs = [_recv_exact(self.sock, n) for n in lens]
        except OSError as e:
            self.close()
            raise ConnectionClosed(str(e)) from e
        self.bytes_in += hlen + sum(lens)
        return header, bufs

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


# ---------------------------------------------------------------- handshake
#
# Mutual HMAC challenge-response over a shared pairing token. Both sides prove
# they hold the token without sending it. Until this succeeds only JSON headers
# are parsed, never pickles.

def _mac(token: str, nonce: str, role: str) -> str:
    return hmac.new(token.encode(), f"{role}:{nonce}".encode(), "sha256").hexdigest()


def server_handshake(conn: Connection, token: str, hello: dict) -> dict:
    """Worker side. Sends hello (with a challenge), checks the client's proof."""
    nonce = os.urandom(16).hex()
    conn.send({"t": "hello", "nonce": nonce, **hello})
    msg, _ = conn.recv()
    if msg.get("t") != "auth" or not hmac.compare_digest(
        msg.get("proof", ""), _mac(token, nonce, "client")
    ):
        conn.send({"t": "auth_fail"})
        conn.close()
        raise PermissionError("client failed pairing-token check")
    conn.send({"t": "auth_ok", "proof": _mac(token, msg["nonce"], "server")})
    return msg


def client_handshake(conn: Connection, token: str, info: dict) -> dict:
    """Mac side. Returns the worker's hello once both sides are verified."""
    hello, _ = conn.recv()
    if hello.get("t") != "hello":
        raise ConnectionClosed(f"unexpected greeting from {conn.name}: {hello.get('t')}")
    nonce = os.urandom(16).hex()
    conn.send({"t": "auth", "proof": _mac(token, hello["nonce"], "client"), "nonce": nonce, **info})
    ok, _ = conn.recv()
    if ok.get("t") != "auth_ok" or not hmac.compare_digest(
        ok.get("proof", ""), _mac(token, nonce, "server")
    ):
        conn.close()
        raise PermissionError(f"{conn.name} rejected the pairing token (or failed to prove it)")
    return hello


Handler = Callable[[dict, list[bytearray]], None]
