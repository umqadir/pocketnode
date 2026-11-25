"""The worker of the App Store edition.

Same runtime as the sideloaded app's worker, with these differences:
- Macs pair at run time (pairing.py): each has its own token, looked up by its
  pair id, and a pairing request is answered while the phone shows a code.
- The compiled packages are fixed when the app is built: nothing is installed,
  removed or restarted at a Mac's request, and no runtime site directory exists.
- The person holding the phone can see who is connected and what is running,
  stop tasks, refuse all work, forget a Mac, and turn Wi-Fi access on and off.
- Every piece of code a Mac sends is kept in a code log the person can read, and
  with review on ("Ask before running new code") a Mac's own new code waits for
  Allow before it runs.
"""

from __future__ import annotations

import ctypes
import hashlib
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .pairing import PairingWindow, PairStore
from .wire import Connection, server_handshake
from .worker import Worker, _Session

FIXED = ("this iPhone runs the App Store edition of pocketnode: its compiled packages are fixed when "
         "the app is built")
PAUSED = "pocketnode is paused on the phone (Accept work is off)"
CODE_LOG_SIZE = 500


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
        pairs.on_replaced = self._drop_pairings  # the same Mac paired again: its old pairing goes
        self.window = PairingWindow()
        self.accepting = True
        self.port: int | None = None
        self.ready = False
        self._task_meta: dict[str, dict] = {}   # task id -> label, fk, code, sid, start
        self._counting: dict[str, str] = {}     # task id -> code hash, until the task's own result goes out
        self.codes: OrderedDict[str, dict] = OrderedDict()  # code hash -> the code log entry (newest last)
        self.review = False                     # "Ask before running new code"
        self.decisions: dict[str, str] = {}     # "<pair_id>:<code>" -> "allowed" | "denied"
        self.waiting: dict[tuple[str, str], dict] = {}  # (pair_id, code) -> a request on screen
        self._approvals_file: Path | None = None
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
            conn, "", self.hello(), admit=self._admit, after_auth=self._after_auth,
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

    def _close_sessions(self, which=lambda s: True, why: str | None = None) -> None:
        """Close sessions; with `why`, say {"t": "bye"} first so the Mac knows it wasn't a crash."""
        with self._lock:
            targets = [s for s in self.sessions.values() if s.live and which(s)]
        if why and targets:
            for s in targets:
                self._send({"t": "bye", "why": why}, sid=s.id)
            self._drain()
        for s in targets:
            if s.conn is not None:
                s.conn.close()

    def _drain(self, timeout: float = 1.0) -> None:
        """Wait (briefly) until the writer has sent what is queued."""
        end = time.monotonic() + timeout
        while not self._outq.empty() and time.monotonic() < end:
            time.sleep(0.01)
        time.sleep(0.02)  # the last item may still be on its way out

    # ---------------------------------------------------------- messages

    def _dispatch(self, sess: _Session, msg: dict, bufs: list[bytearray]) -> None:
        t = msg.get("t")
        if t in ("install", "uninstall"):
            if t == "uninstall" or msg.get("seq", 0) == 0:  # one answer per request
                self._send({"t": "installed", "req": msg.get("req"), "error": FIXED}, sid=sess.id)
            return
        if t == "restart":
            return
        if t == "code_log":  # diagnostics (read-only): what the code log holds, without the texts
            self._send({"t": "code_log", "req": msg.get("req"), "codes": self.code_list(with_digest=True)},
                       sid=sess.id)
            return
        super()._dispatch(sess, msg, bufs)

    def _accept_task(self, msg: dict, bufs: list[bytearray]) -> None:
        fk = msg.get("fk")
        sess = self.sessions.get(msg.get("_sid"))
        pid = str(getattr(sess, "pair_id", None) or "")
        mac = getattr(sess, "mac", None) or "a Mac"
        label = str(msg.get("label") or fk or "task")
        # An older Mac sends no code hash or origin: its code is identified by the function key,
        # and counts as the user's own.
        code = str(msg.get("code") or hashlib.sha256(str(fk).encode()).hexdigest()[:16])
        origin = msg.get("origin") if msg.get("origin") in ("user", "library") else "user"
        src = msg.pop("src", None)
        now = time.time()
        with self._lock:
            e = self.codes.pop(code, None)
            if e is None:
                e = {"code": code, "label": label, "origin": origin, "dist": msg.get("dist"),
                     "where": msg.get("where"), "srckind": msg.get("srckind") or ("source" if src else "none"),
                     "src": "", "macs": [], "pairs": [], "runs": 0, "first": now, "last": now,
                     "state": "not needed"}
            if src:
                e["src"], e["srckind"] = str(src), msg.get("srckind") or "source"
            if mac not in e["macs"]:
                e["macs"].append(mac)
            if pid and pid not in e["pairs"]:
                e["pairs"].append(pid)
            e["last"] = now
            self.codes[code] = e
            while len(self.codes) > CODE_LOG_SIZE:
                self.codes.popitem(last=False)
            refuse = None
            if origin == "user" and self.review:
                d = self.decisions.get(f"{pid}:{code}")
                if d == "denied":
                    e["state"] = "denied"
                    refuse = ("denied", f"{label} was declined on {self.name}")
                elif d != "allowed":
                    e["state"] = "waiting"
                    self.waiting[(pid, code)] = {"code": code, "label": label, "mac": mac, "pair_id": pid,
                                                 "where": e["where"]}
                    refuse = ("approval", f"{label} is waiting for approval on {self.name}")
                else:
                    e["state"] = "allowed"
            else:
                d = self.decisions.get(f"{pid}:{code}") if origin == "user" else None
                e["state"] = d or "not needed"
            if refuse is None:
                self._counting[msg["id"]] = code  # a run counts once it finishes here (_deliver)
                self._task_meta[msg["id"]] = {"label": label, "fk": fk, "code": code,
                                              "sid": msg.get("_sid"), "start": now}
        if refuse is not None:
            self._deliver({"t": "result", "id": msg["id"], "node": self.id, "ok": False,
                           "unavailable": {"kind": refuse[0], "detail": refuse[1], "module": None, "code": code},
                           "state": self.state()}, [], msg.get("_sid"))
            return
        super()._accept_task(msg, bufs)

    # --------------------------------------------------------- code log

    def set_approvals_file(self, path) -> None:
        """Where approvals and denials persist (keyed by pair id and code hash)."""
        self._approvals_file = Path(path)
        try:
            d = json.loads(self._approvals_file.read_text())
            if isinstance(d, dict):
                with self._lock:
                    self.decisions = {str(k): v for k, v in d.items() if v in ("allowed", "denied")}
        except (OSError, ValueError):
            pass

    def _save_decisions(self) -> None:
        if self._approvals_file is None:
            return
        try:
            self._approvals_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._approvals_file.with_name(self._approvals_file.name + ".tmp")
            tmp.write_text(json.dumps(self.decisions))
            os.replace(tmp, self._approvals_file)
        except OSError:
            pass

    def approve(self, code: str, allow: bool) -> int:
        """The person's answer for a piece of code, for every Mac that sent it. Returns how many Macs."""
        code = str(code)
        verdict = "allowed" if allow else "denied"
        with self._lock:
            e = self.codes.get(code)
            pids = set(e["pairs"]) if e else set()
            pids |= {p for p, c in self.waiting if c == code}
            for pid in pids:
                self.decisions[f"{pid}:{code}"] = verdict
                self.waiting.pop((pid, code), None)
            if e is not None and e["origin"] == "user":
                e["state"] = verdict
            sids = [s.id for s in self.sessions.values() if s.live and getattr(s, "pair_id", None) in pids]
            self._save_decisions()
        for sid in sids:
            self._send({"t": "approved" if allow else "denied", "code": code}, sid=sid)
        return len(pids)

    def code_list(self, with_digest: bool = False) -> list[dict]:
        """The code log, newest first, without the texts."""
        with self._lock:
            entries = list(reversed(self.codes.values()))
            out = []
            for e in entries:
                row = {"code": e["code"], "label": e["label"], "origin": e["origin"], "dist": e["dist"],
                       "where": e["where"], "srckind": e["srckind"], "runs": e["runs"], "last": e["last"],
                       "state": e["state"], "macs": list(e["macs"])}
                if with_digest:  # proves which text the phone holds without sending it back
                    row["src_digest"] = hashlib.sha256(e["src"].encode()).hexdigest()[:16] if e["src"] else None
                out.append(row)
            return out

    def code_source(self, code: str) -> str:
        with self._lock:
            e = self.codes.get(str(code))
            return e["src"] if e else ""

    def waiting_info(self) -> list[dict]:
        with self._lock:
            return [dict(w) for w in self.waiting.values()]

    def set_review(self, on: bool) -> None:
        with self._lock:
            self.review = bool(on)
            if not on:
                self.waiting.clear()
                for e in self.codes.values():
                    if e["state"] == "waiting":
                        e["state"] = "not needed"

    def _deliver(self, header: dict, bufs, sid: str | None) -> None:
        # The code log counts a run when the task finishes here with its own result or error;
        # a cancelled, stopped, refused or unavailable task didn't run.
        if header.get("t") == "result":
            with self._lock:
                code = self._counting.pop(header.get("id"), None)
                if code is not None and not header.get("unavailable") and not header.get("infra") \
                        and (header.get("ok") or "error" in header):
                    e = self.codes.get(code)
                    if e is not None:
                        e["runs"] += 1
        super()._deliver(header, bufs, sid)

    def _cleanup(self, tid: str) -> bool:
        cancelled = super()._cleanup(tid)
        with self._lock:
            self._task_meta.pop(tid, None)
            if cancelled:
                self._counting.pop(tid, None)
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
            code = self._task_meta.get(tid, {}).get("code")
        return self.code_source(code) if code else ""

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
            self.stop_tasks()  # the Macs hear about the stopped tasks before the goodbye
            self._close_sessions(why="paused")

    def forget(self, pair_id: str) -> bool:
        """Drop a Mac's pairing (and its approvals); its sessions close and its tasks stop."""
        ok = self.pairs.forget(pair_id)
        self._drop_pairings([pair_id])
        return ok

    def _drop_pairings(self, pair_ids) -> None:
        """A pairing is gone (forgotten, or replaced by the same Mac pairing again): drop its
        approvals, stop its tasks and close its sessions with a goodbye."""
        pair_ids = {str(p) for p in pair_ids}
        with self._lock:
            sids = {s.id for s in self.sessions.values() if getattr(s, "pair_id", None) in pair_ids}
            tids = [t for t, m in self._task_meta.items() if m.get("sid") in sids]
            self.decisions = {k: v for k, v in self.decisions.items() if k.split(":", 1)[0] not in pair_ids}
            self.waiting = {k: v for k, v in self.waiting.items() if k[0] not in pair_ids}
            self._save_decisions()
        for tid in tids:
            self._cancel(tid)
        self._close_sessions(lambda s: s.id in sids, why="forgotten")

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
            self._close_sessions(lambda s: getattr(s, "link", "usb") == "wifi", why="wifi_off")

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
