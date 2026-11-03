"""The worker runtime: runs tasks for a Mac. The same code runs inside the iOS
app (embedded CPython) and as a local process on the Mac.

    python -m pocketnode.worker --listen 127.0.0.1:7151 --token-file ...

One Mac session at a time. A reconnecting Mac replaces the old session, and
results finished while nobody was connected are held and delivered then.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import compat, device, guards, serialize, subinterp
from .errors import TaskCancelled, Unavailable
from .importer import RemoteFinder, install as install_finder
from .store import BufferStore
from .wire import Connection, ConnectionClosed, server_handshake

PROTOCOL = 1
DEFAULT_PORT = 7151


# ------------------------------------------------------------ stdout capture

class _ThreadStream(io.TextIOBase):
    """sys.stdout/err replacement that diverts writes from task threads."""

    def __init__(self, real, slot: str):
        self.real = real
        self.slot = slot
        self.local = threading.local()

    def write(self, s):
        buf = getattr(self.local, "buf", None)
        if buf is None:
            return self.real.write(s) if self.real else len(s)
        buf.write(s)
        return len(s)

    def flush(self):
        if self.real and getattr(self.local, "buf", None) is None:
            self.real.flush()

    @property
    def encoding(self):
        return "utf-8"

    def isatty(self):
        return False


_out = _err = None


def _install_streams():
    global _out, _err
    if _out is None:
        _out = _ThreadStream(sys.stdout, "out")
        _err = _ThreadStream(sys.stderr, "err")
        sys.stdout, sys.stderr = _out, _err


@contextlib.contextmanager
def _captured():
    o, e = io.StringIO(), io.StringIO()
    _out.local.buf, _err.local.buf = o, e
    try:
        yield o, e
    finally:
        _out.local.buf = _err.local.buf = None


# ------------------------------------------------------------------- worker

class Worker:
    def __init__(self, token: str, *, slots: int | None = None, cache_dir: str | None = None,
                 phone: bool | None = None, name: str | None = None, store_budget: int | None = None,
                 node_kind: str | None = None):
        self.token = token
        self.phone = device.IS_IOS if phone is None else phone
        self.kind = node_kind or ("phone" if self.phone else "mac")
        self.name = name or (device.model() if self.phone else f"mac-{os.getpid()}")
        self.id = str(uuid.uuid4())
        self.slots = slots or (os.cpu_count() or 1)
        self.cache_dir = Path(cache_dir or Path(tempfile.gettempdir()) / "pocketnode-cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.scratch = self.cache_dir / "scratch"
        self.scratch.mkdir(exist_ok=True)
        self.store = BufferStore(store_budget or self._default_budget())
        self.pool = ThreadPoolExecutor(self.slots, thread_name_prefix="pn-task")
        self.conn: Connection | None = None
        self.session: str | None = None
        self.lifecycle = "active"
        self.held: dict[str, tuple[dict, list]] = {}
        self.running: dict[str, int] = {}  # task id -> thread ident
        self.cancelled: set[str] = set()
        self._lock = threading.RLock()
        self._pending_imports: dict[str, tuple[threading.Event, list]] = {}
        self._waiting_bufs: dict[str, tuple[dict, list]] = {}
        self._conn_ready = threading.Condition(self._lock)
        self.finder = RemoteFinder(self.cache_dir, self._fetch_module, on_miss=self._on_compiled_miss)
        self.tasks_done = 0
        self.override: dict = {}  # test hook: pretend thermal/lifecycle state
        install_finder(self.finder)
        _install_streams()
        compat.install()
        self._phone_dirs = tuple(
            os.path.realpath(p) for p in {sys.prefix, sys.base_prefix, os.path.dirname(os.__file__),
                                           str(self.finder.cache_dir), *[p for p in sys.path if p]}
        )
        if self.phone:
            roots = [str(self.cache_dir), tempfile.gettempdir(), sys.prefix, sys.base_prefix,
                     os.path.dirname(os.__file__), "/System", "/usr/lib", "/usr/share", "/dev",
                     "/private/var/db/timezone", "/var/db/timezone", "/etc", "/private/etc",
                     os.path.expanduser("~"), *[p for p in sys.path if p and os.path.isabs(p)]]
            guards.install(roots, str(self.scratch), self._phone_dirs)
            os.chdir(self.scratch)
        self.subpool = None
        self.no_sub: set[str] = set()
        if self.phone and subinterp.AVAILABLE and os.environ.get("POCKETNODE_SUBINTERPRETERS", "1") != "0" \
                and getattr(sys, "_is_gil_enabled", lambda: True)():
            self.subpool = subinterp.SubPool(
                self.slots, roots if self.phone else None, str(self.scratch), self._phone_dirs,
                extra_paths=lambda: [str(d) for d in list(self.finder.resolved.values()) if d])

    def _default_budget(self) -> int:
        avail = device.available_memory()
        if avail:
            return int(avail * 0.35)
        return min(device.total_memory() // 8 or (2 << 30), 4 << 30)

    # -------------------------------------------------------- capabilities

    def hello(self) -> dict:
        import importlib.metadata as md
        pkgs = {}
        for d in md.distributions():
            n = d.metadata["Name"]
            if n:
                pkgs[n] = d.version
        return {
            "protocol": PROTOCOL,
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "platform": sys.platform,
            "python": list(sys.version_info[:3]),
            "impl_cache_tag": sys.implementation.cache_tag,
            "gil": getattr(sys, "_is_gil_enabled", lambda: True)(),
            "slots": self.slots,
            "cores": device.core_layout(),
            "model": device.model(),
            "memory": device.total_memory(),
            "packages": pkgs,
            "stdlib_missing": _stdlib_missing(),
            "state": self.state(),
            "store": self.store.stats(),
            "held": list(self.held),
        }

    def state(self) -> dict:
        s = device.snapshot()
        s["lifecycle"] = self.lifecycle
        s["running"] = len(self.running)
        s.update(self.override)
        return s

    def notify(self, lifecycle: str | None = None) -> None:
        """Called by the iOS host on lifecycle changes, or on a timer."""
        if lifecycle:
            self.lifecycle = lifecycle
        self._send({"t": "state", **self.state()})

    # -------------------------------------------------------------- server

    def serve(self, listen: str | None = None, unix: str | None = None,
              connect: str | None = None, ready=None) -> None:
        if connect:  # dial out (used for local workers spawned by the Mac)
            while True:
                try:
                    if connect.startswith("unix:"):
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        s.connect(connect[5:])
                    else:
                        host, port = connect.rsplit(":", 1)
                        s = socket.create_connection((host, int(port)))
                except OSError:
                    return
                self._session(s)
                return
        if unix:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(unix)
            srv.bind(unix)
        else:
            host, port = (listen or f"0.0.0.0:{DEFAULT_PORT}").rsplit(":", 1)
            srv = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((host, int(port)))
        srv.listen(4)
        if ready:
            ready(srv.getsockname())
        threading.Thread(target=self._state_loop, daemon=True, name="pn-state").start()
        while True:
            s, _ = srv.accept()
            threading.Thread(target=self._session, args=(s,), daemon=True, name="pn-session").start()

    def _state_loop(self) -> None:
        last = None
        while True:
            time.sleep(2.0)
            st = self.state()
            key = (st["thermal"], st["lifecycle"], st["low_power"])
            if key != last:
                last = key
                self._send({"t": "state", **st})
            avail = st.get("avail_mem")
            if avail:  # keep the cache well inside what jetsam allows
                self.store.set_budget(int((avail + self.store.size) * 0.35))

    def _session(self, sock: socket.socket) -> None:
        conn = Connection(sock, "mac")
        try:
            info = server_handshake(conn, self.token, self.hello())
        except Exception:
            conn.close()
            return
        with self._lock:
            old = self.conn
            self.conn = conn
            self.session = info.get("session")
            self._conn_ready.notify_all()
        if old is not None:
            old.close()
        if info.get("manifest") is not None:
            self.finder.set_manifest(info["manifest"])
        self._flush_held()
        try:
            while True:
                msg, bufs = conn.recv()
                self._dispatch(msg, bufs)
        except ConnectionClosed:
            pass
        finally:
            with self._lock:
                if self.conn is conn:
                    self.conn = None
            conn.close()

    def _send(self, header: dict, bufs=()) -> bool:
        conn = self.conn
        if conn is None:
            return False
        try:
            conn.send(header, bufs)
            return True
        except ConnectionClosed:
            return False

    def _flush_held(self) -> None:
        with self._lock:
            held, self.held = self.held, {}
        for tid, (h, b) in held.items():
            if not self._send(h, b):
                with self._lock:
                    self.held[tid] = (h, b)

    # ------------------------------------------------------------ messages

    def _dispatch(self, msg: dict, bufs: list[bytearray]) -> None:
        t = msg["t"]
        if t == "task":
            self._accept_task(msg, bufs)
        elif t == "put":
            for h, b in zip(msg["hashes"], bufs):
                self.store.put(h, b)
            self._retry_waiting()
        elif t == "module":
            ev = self._pending_imports.get(msg["req"])
            if ev:
                ev[1].extend([msg, bufs[0] if bufs else None])
                ev[0].set()
        elif t == "manifest":
            self.finder.set_manifest(msg["manifest"])
        elif t == "cancel":
            self._cancel(msg["id"])
        elif t == "pin":
            ok = [h for h in msg["hashes"] if self.store.pin(h)]
            self._send({"t": "pinned", "req": msg.get("req"), "hashes": ok})
        elif t == "unpin":
            for h in msg["hashes"]:
                self.store.unpin(h)
        elif t == "ping":
            self._send({"t": "pong", "ts": msg.get("ts"), "state": self.state(), "store": self.store.stats()})
        elif t == "override_state":
            self.override = msg.get("state", {})
            if "lifecycle" in self.override:
                self.lifecycle = self.override["lifecycle"]
            self._send({"t": "state", **self.state()})
        elif t == "drop_cache":
            self.store.clear()
        elif t == "echo":  # throughput probe
            self._send({"t": "echo", "n": sum(len(b) for b in bufs)}, bufs if msg.get("back") else ())
        elif t == "exec":  # diagnostics: run a snippet, return repr
            self._accept_task({"id": msg["id"], "bufs": [], "diag": True}, bufs)

    def _accept_task(self, msg: dict, bufs: list[bytearray]) -> None:
        # bufs[0] is the pickle; then inline buffers for specs with "i": 1.
        specs = msg.get("bufs", [])
        it = iter(bufs[1:])
        for s in specs:
            if s.get("i"):
                self.store.put(s["h"], next(it))
        need = self.store.missing(s["h"] for s in specs)
        if need:
            with self._lock:
                self._waiting_bufs[msg["id"]] = (msg, bufs[:1])
            self._send({"t": "need", "id": msg["id"], "hashes": need})
            return
        self._start(msg, bufs[0])

    def _retry_waiting(self) -> None:
        with self._lock:
            waiting = list(self._waiting_bufs.items())
        for tid, (msg, b) in waiting:
            if not self.store.missing(s["h"] for s in msg.get("bufs", [])):
                with self._lock:
                    if self._waiting_bufs.pop(tid, None) is None:
                        continue
                self._start(msg, b[0])

    def _start(self, msg: dict, payload: bytearray) -> None:
        self.pool.submit(self._run_safe, msg, payload)

    def _run_safe(self, msg: dict, payload: bytearray) -> None:
        # The thread pool swallows exceptions; a lost result would hang the
        # Mac's future forever, so anything unexpected becomes an infra error.
        try:
            self._run(msg, payload)
        except BaseException as exc:  # noqa: BLE001
            with self._lock:
                self.running.pop(msg["id"], None)
            header = {"t": "result", "id": msg["id"], "node": self.id, "ok": False, "infra": True,
                      "error": _format_exc(exc), "exc_type": type(exc).__name__}
            if not self._send(header):
                with self._lock:
                    self.held[msg["id"]] = (header, [])

    def _cancel(self, tid: str) -> None:
        with self._lock:
            self.cancelled.add(tid)
            self._waiting_bufs.pop(tid, None)
            ident = self.running.get(tid)
        if ident:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident), ctypes.py_object(TaskCancelled))

    # ----------------------------------------------------------- execution

    def _run(self, msg: dict, payload: bytearray) -> None:
        tid = msg["id"]
        if tid in self.cancelled:
            return
        hashes = [s["h"] for s in msg.get("bufs", [])]
        t0 = time.perf_counter()
        held = self.store.acquire(hashes)
        header = {"t": "result", "id": tid, "node": self.id}
        out_bufs: list = []
        with self._lock:
            self.running[tid] = threading.get_ident()
        try:
            if held is None:
                raise Unavailable("cache", "input buffers were evicted before the task started")
            # Each task gets its own writable copies unless the caller allowed
            # sharing read-only views (joblib passes read-only memmaps anyway).
            ro = msg.get("ro")
            views = [memoryview(b).toreadonly() if ro is not None and len(b) >= ro else bytearray(b)
                     for b in held]
            t1 = time.perf_counter()
            if self.subpool is not None and msg.get("pure") and msg.get("fk") not in self.no_sub:
                done = self._run_sub(msg, payload, views, header, t0, t1)
                if done is not None:
                    out_bufs = done
                    raise _Sent
            fn, args, kwargs = serialize.loads(payload, views)
            t2 = time.perf_counter()
            guards.begin_task()
            with _captured() as (o, e):
                try:
                    value = fn(*args, **kwargs)
                    raised = None
                except guards._Escape as esc:
                    value, raised = None, esc.err
                except TaskCancelled:
                    raise
                except BaseException as exc:  # noqa: BLE001 - shipped back to the Mac
                    value, raised = None, exc
            trip = guards.end_task()
            t3 = time.perf_counter()
            missing = self._missing_from(raised)
            if trip is not None or isinstance(raised, Unavailable) or missing:
                u = trip or (raised if isinstance(raised, Unavailable) else missing)
                header.update(ok=False, unavailable={"kind": u.kind, "detail": u.detail, "module": u.module})
            elif raised is not None:
                header.update(ok=False, error=_format_exc(raised), exc_type=type(raised).__name__)
                try:
                    p = serialize.dumps_plain(raised)
                    out_bufs = [p.data, *p.buffers]
                except Exception:
                    out_bufs = []
            else:
                p = serialize.dumps_plain(value)
                out_bufs = [p.data, *p.buffers]
                header["ok"] = True
            t4 = time.perf_counter()
            header.update(
                stdout=o.getvalue()[-200_000:], stderr=e.getvalue()[-200_000:],
                timing={"prep": t1 - t0, "load": t2 - t1, "run": t3 - t2, "dump": t4 - t3},
                fetched=dict(self.finder.fetched), missing=list(self.finder.missing_compiled),
            )
        except _Sent:
            pass
        except TaskCancelled:
            return
        except Unavailable as u:
            header.update(ok=False, unavailable={"kind": u.kind, "detail": u.detail, "module": u.module})
        except BaseException as exc:  # failure to load the task itself
            header.update(ok=False, error=_format_exc(exc), exc_type=type(exc).__name__, infra=True)
            missing = self._missing_from(exc)
            if missing:
                header["unavailable"] = {"kind": missing.kind, "detail": missing.detail, "module": missing.module}
        finally:
            with self._lock:
                self.running.pop(tid, None)
                cancelled = tid in self.cancelled
                self.cancelled.discard(tid)
            self.store.release(hashes)
        if cancelled:
            return
        header["evicted"] = self.store.take_evicted()
        header["state"] = self.state()
        self.tasks_done += 1
        if not self._send(header, out_bufs):
            with self._lock:
                self.held[tid] = (header, out_bufs)

    def _run_sub(self, msg, payload, views, header, t0, t1) -> list | None:
        """Run a pure task in a subinterpreter. None means: run it in the main interpreter."""
        try:
            out = self.subpool.run(payload, views)
        except Exception as e:  # noqa: BLE001 - infrastructure trouble: fall back
            out = {"status": "needs-main", "detail": repr(e)}
        t3 = time.perf_counter()
        status = out["status"]
        if status == "needs-main":
            self.no_sub.add(msg.get("fk"))
            return None
        bufs: list = []
        if status == "ok":
            header["ok"] = True
            bufs = [out["value"]]
        elif status == "unavailable":
            kind, detail, module = out["u"]
            header.update(ok=False, unavailable={"kind": kind, "detail": detail, "module": module})
        else:
            header.update(ok=False, error=out.get("tb", ""), exc_type=out.get("etype", "Error"))
            bufs = [out["exc"]] if "exc" in out else []
        header.update(stdout=out.get("stdout", ""), stderr=out.get("stderr", ""), sub=1,
                      timing={"prep": t1 - t0, "load": 0.0, "run": t3 - t1, "dump": 0.0},
                      fetched=dict(self.finder.fetched), missing=list(self.finder.missing_compiled))
        return bufs

    def _missing_from(self, exc) -> Unavailable | None:
        """An ImportError for a compiled module the Mac has but this node lacks."""
        if not isinstance(exc, ImportError):
            return None
        name = (getattr(exc, "name", None) or "").split(".")[0]
        info = self.finder.manifest.get(name)
        if name and (name in self.finder.missing_compiled or (info and info["kind"] == "compiled")):
            return Unavailable("module", f"{name} is compiled and not installed on {self.name}", module=name)
        return None

    # --------------------------------------------------------- import fetch

    def _on_compiled_miss(self, top: str) -> None:
        # Library code probing optional accelerators (pandas trying numexpr) is
        # fine; the user's own code branching on a missing module is not.
        if guards.in_task() and guards.user_frame_import(top, self._phone_dirs):
            guards.trip("module", f"your code imports {top}, which is compiled and not on {self.name}",
                        module=top, raise_=False)

    def _fetch_module(self, name: str, have: str | None):
        req = uuid.uuid4().hex
        ev = (threading.Event(), [])
        self._pending_imports[req] = ev
        try:
            if not self._send({"t": "import", "req": req, "name": name, "have": have}):
                raise ConnectionClosed("no Mac connected")
            if not ev[0].wait(120):
                raise TimeoutError(f"Mac didn't answer import of {name}")
            msg, data = ev[1]
            info = msg["info"]
            if msg.get("same"):
                return info, None
            return info, bytes(data) if data is not None else None
        finally:
            self._pending_imports.pop(req, None)


class _Sent(Exception):
    """Internal: the task already ran in a subinterpreter."""


def _format_exc(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


_IOS_MISSING = ("tkinter", "curses", "readline", "_curses", "_tkinter", "dbm", "_gdbm", "nis", "spwd")


def _stdlib_missing() -> list[str]:
    import importlib.util
    out = []
    for m in _IOS_MISSING:
        try:
            if importlib.util.find_spec(m) is None:
                out.append(m)
        except Exception:
            out.append(m)
    return out


def _read_token(path: str | None) -> str:
    if path:
        return Path(path).read_text().strip()
    return os.environ["POCKETNODE_TOKEN"]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m pocketnode.worker")
    ap.add_argument("--listen", help="host:port to listen on")
    ap.add_argument("--unix", help="unix socket path to listen on")
    ap.add_argument("--connect", help="dial out to host:port or unix:path")
    ap.add_argument("--token-file")
    ap.add_argument("--slots", type=int)
    ap.add_argument("--cache-dir")
    ap.add_argument("--phone", action="store_true", help="behave like the phone (guards, phone kind)")
    ap.add_argument("--name")
    ap.add_argument("--kind")
    a = ap.parse_args(argv)
    w = Worker(_read_token(a.token_file), slots=a.slots, cache_dir=a.cache_dir,
               phone=a.phone or None, name=a.name, node_kind=a.kind)
    w.serve(listen=a.listen, unix=a.unix, connect=a.connect,
            ready=lambda addr: print(json.dumps({"ready": str(addr)}), file=sys.__stdout__, flush=True))


if __name__ == "__main__":
    main()
