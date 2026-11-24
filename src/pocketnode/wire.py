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


# Until a peer has proved it holds the pairing token, it may only send small
# JSON frames: nothing it says can make us allocate much or unpickle anything.
_PREAUTH_HEADER_MAX = 4 << 20
HANDSHAKE_TIMEOUT = 15.0


class ConnectionClosed(ConnectionError):
    pass


class Refused(ConnectionError):
    """The worker is paired with us but can't take this session right now."""


_TREE_MIN = 8 << 20
_TREE_CHUNK = 4 << 20
_hash_pool = None


def digest(buf) -> str:
    """Content address of a buffer (hardware-accelerated SHA-256 on Apple chips).

    Large buffers are hashed as fixed 4 MB chunks in parallel (hashlib releases
    the GIL) and the chunk digests are hashed together, so a 1 GB array costs a
    fraction of a second instead of most of one. The chunking is fixed, so the
    Mac and the phone always compute the same address.
    """
    view = memoryview(buf)
    if view.nbytes < _TREE_MIN:
        return hashlib.sha256(view).hexdigest()[:32]
    global _hash_pool
    if _hash_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _hash_pool = ThreadPoolExecutor(min(8, os.cpu_count() or 2), thread_name_prefix="pn-hash")
    view = view.cast("B") if view.format != "B" or view.ndim != 1 else view
    parts = _hash_pool.map(lambda i: hashlib.sha256(view[i:i + _TREE_CHUNK]).digest(),
                           range(0, view.nbytes, _TREE_CHUNK))
    top = hashlib.sha256(b"pn-tree:%d:" % view.nbytes)
    for d in parts:
        top.update(d)
    return top.hexdigest()[:32]


def tune(sock: socket.socket) -> None:
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Kernel keepalive notices a peer that vanished without closing (Wi-Fi
        # dropping out) within ~25 s, and unlike a ping from Python it keeps
        # working while a task holds the GIL.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPALIVE", 0x10), 10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        except (OSError, AttributeError):
            pass
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
        self.authenticated = False
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
            if not self.authenticated and (nbuf or hlen > _PREAUTH_HEADER_MAX):
                raise ConnectionClosed(f"oversized frame from {self.name} before pairing")
            lens = struct.unpack(f"!{nbuf}Q", _recv_exact(self.sock, 8 * nbuf)) if nbuf else ()
            header = json.loads(_recv_exact(self.sock, hlen))
            bufs = [_recv_exact(self.sock, n) for n in lens]
        except (OSError, ValueError, struct.error) as e:
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


def server_handshake(conn: Connection, token: str, hello: dict, admit=None, *,
                     token_for=None, on_pair=None) -> dict:
    """Worker side. Sends hello (with a challenge), checks the client's proof.

    admit(msg) may return a reason to turn a correctly paired client away
    (the phone is serving a different Python environment, say).

    App Store edition only: token_for(msg) looks the token up by the client's
    pair_id (None: not paired), and on_pair(conn, msg, nonce) answers a
    {"t": "pair"} request, after which the connection ends.
    """
    conn.sock.settimeout(HANDSHAKE_TIMEOUT)
    nonce = os.urandom(16).hex()
    conn.send({"t": "hello", "nonce": nonce, **hello})
    msg, _ = conn.recv()
    if on_pair is not None and msg.get("t") == "pair":
        try:
            on_pair(conn, msg, nonce)
        finally:
            conn.close()
        raise ConnectionClosed("pairing request answered")
    if token_for is not None:
        token = token_for(msg) if msg.get("t") == "auth" else None
        if token is None:
            conn.send({"t": "auth_fail", "why": "unpaired"})
            conn.close()
            raise PermissionError("client isn't paired with this phone")
    if msg.get("t") != "auth" or not hmac.compare_digest(
        str(msg.get("proof", "")), _mac(token, nonce, "client")
    ):
        conn.send({"t": "auth_fail"})
        conn.close()
        raise PermissionError("client failed pairing-token check")
    proof = _mac(token, str(msg.get("nonce", "")), "server")
    why = admit(msg) if admit else None
    if why:
        conn.send({"t": "refused", "proof": proof, "reason": why})
        conn.close()
        raise Refused(why)
    conn.send({"t": "auth_ok", "proof": proof})
    conn.authenticated = True
    conn.sock.settimeout(None)
    return msg


def client_handshake(conn: Connection, token: str, info: dict, credentials=None) -> dict:
    """Mac side. Returns the worker's hello once both sides are verified.

    A phone running the App Store edition says so in its hello (pairing:
    "runtime"); then credentials(hello) supplies (token, extra auth fields),
    pairing.credentials_for by default, and `token` is not used.
    """
    conn.sock.settimeout(HANDSHAKE_TIMEOUT)
    runtime = False
    try:
        hello, _ = conn.recv()
        if hello.get("t") != "hello":
            raise ConnectionClosed(f"unexpected greeting from {conn.name}: {hello.get('t')}")
        if hello.get("pairing") == "runtime":
            from . import pairing
            runtime = True
            cred = (credentials or pairing.credentials_for)(hello)
            if cred is None:
                conn.close()
                raise pairing.NotPaired(f"this Mac isn't paired with {hello.get('name', conn.name)}")
            token, extra = cred
            info = {**info, **extra}
        elif callable(token):  # the dev token, only made when a phone actually needs it
            token = token()
        nonce = os.urandom(16).hex()
        conn.send({"t": "auth", "proof": _mac(token, str(hello.get("nonce", "")), "client"), "nonce": nonce, **info})
        ok, _ = conn.recv()
    except TimeoutError as e:
        conn.close()
        raise ConnectionClosed(f"{conn.name} didn't answer the handshake") from e
    if runtime and ok.get("t") == "auth_fail" and ok.get("why") == "unpaired":
        from . import pairing
        conn.close()
        raise pairing.PairingRevoked(f"{hello.get('name', conn.name)} no longer has this Mac's pairing")
    proved = hmac.compare_digest(str(ok.get("proof", "")), _mac(token, nonce, "server"))
    if ok.get("t") == "refused" and proved:
        conn.close()
        e = Refused(ok.get("reason", "the worker refused this session"))
        e.hello = hello  # which phone refused (the App Store edition says so in its hello)
        raise e
    if ok.get("t") != "auth_ok" or not proved:
        conn.close()
        raise PermissionError(f"{conn.name} rejected the pairing token (or failed to prove it)")
    conn.authenticated = True
    conn.sock.settimeout(None)
    return hello


Handler = Callable[[dict, list[bytearray]], None]
