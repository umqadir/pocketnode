"""The worker of the App Store edition.

Same runtime as the sideloaded app's worker, with these differences:
- Macs pair at run time (pairing.py): each has its own token, looked up by its
  pair id, and a pairing request is answered while the phone shows a code.
- The compiled packages are fixed when the app is built: nothing is installed,
  removed or restarted at a Mac's request, and no runtime site directory exists.
- The person holding the phone can see who is connected and what is running,
  stop tasks, refuse all work, forget a Mac, and turn Wi-Fi access on and off.
"""

from __future__ import annotations

import ctypes
import ipaddress
import socket
import sys
import threading
import time

from .pairing import PairingWindow, PairStore
from .wire import Connection, server_handshake
from .worker import Worker, _Session

FIXED = ("this iPhone runs the App Store edition of pocketnode: its compiled packages are fixed when "
         "the app is built")
PAUSED = "pocketnode is paused on the phone (Accept work is off)"


def _ssl_missing() -> bool:
    try:
        import ssl  # noqa: F401
        return False
    except ImportError:
        return True


class StoreWorker(Worker):
    edition = "store"
    uses_site = False

    def __init__(self, pairs: PairStore, **kw):
        self.pairs = pairs
        self.window = PairingWindow()
        self.accepting = True
        self.port: int | None = None
        self.ready = False
        self._task_meta: dict[str, dict] = {}   # task id -> label, fk, sid, start
        self._src: dict[str, str] = {}          # function key -> its source text, as the Mac sent it
        self._wifi_srv: socket.socket | None = None
        self._wifi_wanted = False
        self._no_ssl = _ssl_missing()
        super().__init__("", **kw)

    # ----------------------------------------------------------- hello

    def hello(self) -> dict:
        h = super().hello()
        h.update(edition="store", pairing="runtime", phone_id=self.pairs.phone_id, fixed_packages=True,
                 disk_free=None, site=None)
        if self._no_ssl:  # a build without TLS: tasks that need it stay on the Mac
            h["stdlib_missing"] = sorted(set(h.get("stdlib_missing", ())) | {"ssl", "_ssl"})
        return h

    # ------------------------------------------------------- sessions

    @staticmethod
    def _peer_is_local(sock: socket.socket) -> bool:
        """A loopback or unix-socket peer: on the phone that means the USB cable (usbmuxd)."""
        if sock.family == getattr(socket, "AF_UNIX", None):
            return True
        try:
            host = sock.getpeername()[0]
            ip = ipaddress.ip_address(str(host).split("%")[0])
        except (OSError, ValueError, IndexError):
            return False
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return ip.is_loopback

    def _handshake(self, conn: Connection) -> dict:
        local = self._peer_is_local(conn.sock)
        return server_handshake(
            conn, "", self.hello(), admit=self._admit,
            token_for=lambda msg: self.pairs.token(msg.get("pair_id")),
            on_pair=lambda c, msg, nonce: self.window.handle(c, msg, nonce, self.pairs, local, phone_name=self.name))

    def _session_started(self, sess: _Session, conn: Connection, info: dict) -> None:
        pid = info.get("pair_id")
        sess.pair_id = pid
        sess.mac = self.pairs.name(pid) or "a Mac"
        sess.link = "usb" if self._peer_is_local(conn.sock) else "wifi"
        sess.since = time.time()
        self.pairs.touch(pid)

    def _admit(self, msg: dict) -> str | None:
        if not self.accepting:
            return PAUSED
        return super()._admit(msg)

    def sessions_info(self) -> list[dict]:
        with self._lock:
            live = [s for s in self.sessions.values() if s.live]
            return [{"mac": getattr(s, "mac", "a Mac"), "pair_id": getattr(s, "pair_id", None),
                     "link": getattr(s, "link", "usb"), "since": getattr(s, "since", None)} for s in live]

    def _close_sessions(self, which=lambda s: True) -> None:
        with self._lock:
            conns = [s.conn for s in self.sessions.values() if s.live and which(s)]
        for c in conns:
            c.close()

    # ---------------------------------------------------------- messages

    def _dispatch(self, sess: _Session, msg: dict, bufs: list[bytearray]) -> None:
        t = msg.get("t")
        if t in ("install", "uninstall"):
            if t == "uninstall" or msg.get("seq", 0) == 0:  # one answer per request
                self._send({"t": "installed", "req": msg.get("req"), "error": FIXED}, sid=sess.id)
            return
        if t == "restart":
            return
        super()._dispatch(sess, msg, bufs)

    def _accept_task(self, msg: dict, bufs: list[bytearray]) -> None:
        fk = msg.get("fk")
        src = msg.pop("src", None)
        with self._lock:
            if src and fk:
                if len(self._src) > 500:
                    self._src.pop(next(iter(self._src)))
                self._src[fk] = str(src)
            self._task_meta[msg["id"]] = {"label": msg.get("label") or fk or "task", "fk": fk,
                                          "sid": msg.get("_sid"), "start": time.time()}
        super()._accept_task(msg, bufs)

    def _cleanup(self, tid: str) -> bool:
        cancelled = super()._cleanup(tid)
        with self._lock:
            self._task_meta.pop(tid, None)
        return cancelled

    # ------------------------------------------------------------ tasks

    def tasks_info(self) -> list[dict]:
        now = time.time()
        with self._lock:
            out = []
            for tid, m in self._task_meta.items():
                sess = self.sessions.get(m.get("sid"))
                out.append({"id": tid, "label": m["label"], "mac": getattr(sess, "mac", None) or "a Mac",
                            "elapsed": round(now - m["start"], 1),
                            "where": "sub" if tid in self._sub_cancels else "main"})
            return out

    def task_source(self, tid: str) -> str:
        with self._lock:
            fk = self._task_meta.get(tid, {}).get("fk")
            return self._src.get(fk, "") if fk else ""

    def stop_tasks(self, which=lambda meta: True) -> int:
        """Cancel tasks in flight; each Mac hears they were stopped and runs them itself."""
        with self._lock:
            items = [(tid, m.get("sid")) for tid, m in self._task_meta.items() if which(m)]
        for tid, sid in items:
            self._cancel(tid)
            self._deliver({"t": "result", "id": tid, "node": self.id, "ok": False,
                           "unavailable": {"kind": "stopped", "detail": f"stopped on {self.name} by its owner",
                                           "module": None},
                           "state": self.state()}, [], sid)
        return len(items)

    def set_accepting(self, on: bool) -> None:
        self.accepting = bool(on)
        if not on:
            self.stop_tasks()
            end = time.monotonic() + 1.0  # let the Macs hear about the stopped tasks first
            while not self._outq.empty() and time.monotonic() < end:
                time.sleep(0.01)
            self._close_sessions()

    def forget(self, pair_id: str) -> bool:
        """Drop a Mac's pairing; its sessions close and its tasks stop."""
        ok = self.pairs.forget(pair_id)
        with self._lock:
            sids = {s.id for s in self.sessions.values() if getattr(s, "pair_id", None) == pair_id}
            tids = [t for t, m in self._task_meta.items() if m.get("sid") in sids]
        for tid in tids:
            self._cancel(tid)
        self._close_sessions(lambda s: s.id in sids)
        return ok

    # ------------------------------------------------------------ network

    def serve(self, listen=None, unix=None, connect=None, ready=None) -> None:
        def on_ready(addr):
            if isinstance(addr, tuple) and len(addr) >= 2:
                self.port = int(addr[1])
            self.ready = True
            if self._wifi_wanted:
                self.set_wifi(True)
            if ready:
                ready(addr)

        try:
            super().serve(listen=listen, unix=unix, connect=connect, ready=on_ready)
        finally:
            self.ready = False

    def set_wifi(self, on: bool) -> None:
        """Accept Macs over the local network too (an extra listener on all addresses)."""
        with self._lock:
            self._wifi_wanted = bool(on)
            if on and self._wifi_srv is None and self.port is not None:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    srv.bind(("0.0.0.0", self.port))
                except OSError:
                    srv.close()
                    raise
                srv.listen(8)
                self._wifi_srv = srv
                threading.Thread(target=self._accept_loop, args=(srv,), daemon=True, name="pn-accept-wifi").start()
            elif not on and self._wifi_srv is not None:
                srv, self._wifi_srv = self._wifi_srv, None
                srv.close()
        if not on:
            self._close_sessions(lambda s: getattr(s, "link", "usb") == "wifi")

    @property
    def wifi(self) -> bool:
        return self._wifi_srv is not None

    def wifi_address(self) -> str | None:
        if self._wifi_srv is None or self.port is None:
            return None
        ip = lan_ip()
        return f"{ip}:{self.port}" if ip else None


# ------------------------------------------------------------ LAN address

class _sockaddr(ctypes.Structure):
    _fields_ = [("sa_len", ctypes.c_uint8), ("sa_family", ctypes.c_uint8), ("sa_data", ctypes.c_uint8 * 14)]


class _ifaddrs(ctypes.Structure):
    pass


_ifaddrs._fields_ = [("ifa_next", ctypes.POINTER(_ifaddrs)), ("ifa_name", ctypes.c_char_p),
                     ("ifa_flags", ctypes.c_uint), ("ifa_addr", ctypes.POINTER(_sockaddr)),
                     ("ifa_netmask", ctypes.POINTER(_sockaddr)), ("ifa_dstaddr", ctypes.POINTER(_sockaddr)),
                     ("ifa_data", ctypes.c_void_p)]


def lan_ip() -> str | None:
    """This device's IPv4 address on the local network (Wi-Fi or Ethernet), if it has one."""
    found: list[tuple[str, str]] = []
    if sys.platform in ("darwin", "ios"):
        try:
            libc = ctypes.CDLL(None)
            head = ctypes.POINTER(_ifaddrs)()
            if libc.getifaddrs(ctypes.byref(head)) == 0:
                try:
                    p = head
                    while p:
                        ifa = p.contents
                        name = (ifa.ifa_name or b"").decode(errors="replace")
                        up, loop = ifa.ifa_flags & 0x1, ifa.ifa_flags & 0x8
                        if ifa.ifa_addr and up and not loop and ifa.ifa_addr.contents.sa_family == socket.AF_INET:
                            # sockaddr_in: len, family, port (2 bytes), then the address
                            ip = ".".join(str(b) for b in ifa.ifa_addr.contents.sa_data[2:6])
                            if not ip.startswith("169.254."):
                                found.append((name, ip))
                        p = ifa.ifa_next
                finally:
                    libc.freeifaddrs(head)
        except (OSError, AttributeError, ValueError):
            pass
    for name, ip in sorted(found, key=lambda f: (not f[0].startswith("en"), f[0])):
        return ip
    try:  # elsewhere: the address the default route would use
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return None if ip.startswith(("127.", "0.")) else ip
    except OSError:
        return None
