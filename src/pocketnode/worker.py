"""The worker runtime: runs tasks for a Mac. The same code runs inside the iOS
app (embedded CPython) and as a local process on the Mac.

    python -m pocketnode.worker --listen 127.0.0.1:7151 --token-file ...

Several Mac sessions (executors) can be connected at once as long as they share
one Python environment. Each task's result goes back to the session that sent
it; if that session is disconnected, the result is held until it reconnects.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import queue
import ctypes
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import compat, device, envinfo, guards, serialize, subinterp
from .errors import TaskCancelled, Unavailable
from .importer import RemoteFinder, _local_versions, _norm, install as install_finder
from .store import BufferStore
from .wire import Connection, ConnectionClosed, digest, server_handshake

PROTOCOL = 1
DEFAULT_PORT = 7151
STALE = 1800  # seconds a Mac session may stay away before what it left here is freed
SHIMS = "_pocketnode_shims"  # shared by every added package: merged, never replaced


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

class _Session:
    """One Mac-side executor. It may disconnect and come back; its results wait for it."""

    def __init__(self, sid: str):
        self.id = sid
        self.conn: Connection | None = None
        self.held: dict[str, tuple[dict, list]] = {}
        self.env: str | None = None
        self.seen = time.monotonic()

    @property
    def live(self) -> bool:
        return self.conn is not None and not self.conn.closed


def _env_key(manifest) -> str | None:
    if manifest is None:
        return None
    import hashlib
    return hashlib.sha1(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


class Worker:
    uses_site = True  # packages added at run time are imported from self.site
    # What this worker understands beyond plain tasks; a Mac checks before using one.
    features = ("disk-cache", "pinned-objects", "objects", "stream")

    def __init__(self, token: str, *, slots: int | None = None, cache_dir: str | None = None,
                 phone: bool | None = None, name: str | None = None, store_budget: int | None = None,
                 node_kind: str | None = None, site_dir: str | None = None):
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
        # Packages added while the app runs. First on sys.path, so an added
        # version wins over one bundled with the app.
        self.site = Path(site_dir or self.cache_dir / "site")
        self.site.mkdir(parents=True, exist_ok=True)
        if self.uses_site and str(self.site) not in sys.path:
            sys.path.insert(0, str(self.site))
        self._installs: dict[str, set[str] | None] = {}  # req -> modules loaded before it (None: failed)
        # A phone keeps large inputs on disk too, so they outlive an app restart. (A worker on
        # the Mac gets them over a local socket faster than it could read them back.)
        self.store = BufferStore(store_budget or self._default_budget(),
                                 disk_dir=self.cache_dir / "buffers" if self.kind != "mac" else None)
        self.pool = ThreadPoolExecutor(self.slots, thread_name_prefix="pn-task")
        self.sessions: dict[str, _Session] = {}
        self.env: str | None = None  # the Mac environment the import hook currently mirrors
        self.lifecycle = "active"
        self.last_crash: str | None = None  # set by the iOS host from the previous run's fault log
        self.inflight: set[str] = set()      # accepted and not yet finished
        self.running: dict[str, int] = {}    # task id -> thread ident, only while user code runs
        self.cancelled: set[str] = set()
        self._sub_cancels: dict[str, Callable[[], None]] = {}  # task id -> interrupts its subinterpreter run
        self._reserved: dict[str, list[str]] = {}  # task id -> buffer hashes it holds in the store
        self.kept: dict[str, dict] = {}  # key -> an object pinned from a Mac (ex.pin), built on first use
        # Objects a Mac built here (ex.create), by the id it chose. They live in the main
        # interpreter; their method calls run on the task threads, one at a time per object.
        self.objects: dict[str, dict] = {}
        self._acks: dict[str, list[int]] = {}  # streaming task id -> [items the Mac has taken]
        self._ack_cv = threading.Condition()
        self._lock = threading.RLock()
        self._tl = threading.local()
        self._pending_imports: dict[str, tuple[threading.Event, list]] = {}
        self._waiting_bufs: dict[str, tuple[dict, bytearray, set]] = {}
        self._prefetched: set[str] = set()
        self.finder = RemoteFinder(self.cache_dir, self._fetch_module, on_miss=self._on_compiled_miss)
        self.tasks_done = 0
        self.override: dict = {}  # test hook: pretend thermal/lifecycle state
        self._outq: queue.Queue = queue.Queue()
        threading.Thread(target=self._writer, daemon=True, name="pn-writer").start()
        install_finder(self.finder)
        _install_streams()
        compat.install()
        self._phone_dirs = tuple(
            os.path.realpath(p) for p in {sys.prefix, sys.base_prefix, os.path.dirname(os.__file__),
                                           str(self.finder.cache_dir), str(self.site), *[p for p in sys.path if p]}
        )
        roots = None
        if self.phone:
            roots = [str(self.cache_dir), str(self.site), tempfile.gettempdir(), sys.prefix, sys.base_prefix,
                     os.path.dirname(os.__file__), "/System", "/usr/lib", "/usr/share", "/dev",
                     "/private/var/db/timezone", "/var/db/timezone", "/etc", "/private/etc",
                     os.path.expanduser("~"), *[p for p in sys.path if p and os.path.isabs(p)]]
            guards.install(roots, str(self.scratch), self._phone_dirs)
            # The working directory is empty and read-only. Python-level relative
            # paths are caught by the guards; this covers C and C++ code
            # (pyarrow, HDF5, ...) too: a relative read finds nothing and a
            # relative write fails, so the task goes to the Mac, where the path
            # means what the user meant, instead of quietly writing here.
            cwd = self.cache_dir / "cwd"
            cwd.mkdir(exist_ok=True)
            os.chmod(cwd, 0o555)
            os.chdir(cwd)
        self.subpool = None
        self.no_sub: dict[str, int] = {}
        self.sub_fallbacks: dict[str, str] = {}  # why a function left the subinterpreters (diagnostics)
        if self.phone and subinterp.AVAILABLE and os.environ.get("POCKETNODE_SUBINTERPRETERS", "1") != "0" \
                and getattr(sys, "_is_gil_enabled", lambda: True)():
            self.subpool = subinterp.SubPool(
                self.slots, roots, str(self.scratch), self._phone_dirs, extra_paths=self.finder.paths)

    def _default_budget(self) -> int:
        avail = device.available_memory()
        if avail:
            return int(avail * 0.35)
        return min(device.total_memory() // 8 or (2 << 30), 4 << 30)

    @property
    def connected(self) -> bool:
        return any(s.live for s in self.sessions.values())

    # -------------------------------------------------------- capabilities

    def packages(self) -> dict[str, str]:
        import importlib.metadata as md
        pkgs, seen = {}, set()
        for d in md.distributions():
            n = d.metadata["Name"]
            # The first copy on sys.path is the one that imports (an added one before a bundled one).
            if n and _norm(n) not in seen:
                seen.add(_norm(n))
                pkgs[n] = d.version
        return pkgs

    def hello(self) -> dict:
        return {
            "protocol": PROTOCOL,
            "features": list(self.features),
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "platform": sys.platform,
            "python": list(sys.version_info[:3]),
            "impl_cache_tag": sys.implementation.cache_tag,
            "gil": getattr(sys, "_is_gil_enabled", lambda: True)(),
            "subinterpreters": self.subpool is not None,
            "slots": self.slots,
            "cores": device.core_layout(),
            "model": device.model(),
            "memory": device.total_memory(),
            "packages": self.packages(),
            "stdlib_missing": _stdlib_missing(),
            "state": self.state(),
            "store": self.store.stats(),
            "last_crash": self.last_crash,
            "source": envinfo.source_version(),
            "simulator": bool(os.environ.get("SIMULATOR_UDID")),
            "site": str(self.site),
            "disk_free": shutil.disk_usage(self.site).free,
        }

    def state(self) -> dict:
        s = device.snapshot()
        s["lifecycle"] = self.lifecycle
        s["running"] = len(self.inflight)
        s["continued"] = bool(getattr(self, "continued", False))  # iOS background computing is armed
        if self.sub_fallbacks:
            s["sub_fallbacks"] = dict(list(self.sub_fallbacks.items())[-8:])
        s.update(self.override)
        return s

    def notify(self, lifecycle: str | None = None) -> None:
        """Called by the iOS host on lifecycle changes, or on a timer."""
        if lifecycle:
            self.lifecycle = lifecycle
        self._send({"t": "state", **self.state()})

    # -------------------------------------------------------------- server

    def serve(self, listen: str | list[str] | None = None, unix: str | None = None,
              connect: str | None = None, ready=None) -> None:
        if connect:  # dial out (used for local workers spawned by the Mac)
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
        servers = []
        if unix:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(unix)
            srv.bind(unix)
            servers.append(srv)
        else:
            addrs = [listen] if isinstance(listen, str) else (listen or [f"0.0.0.0:{DEFAULT_PORT}"])
            for a in addrs:
                host, port = a.rsplit(":", 1)
                host = host.strip("[]")
                srv = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    srv.bind((host, int(port)))
                except OSError:
                    if not servers and a == addrs[-1]:
                        raise
                    continue  # e.g. no IPv6 loopback: the other address is enough
                servers.append(srv)
        for srv in servers:
            srv.listen(8)
        if ready:
            ready(servers[0].getsockname())
        threading.Thread(target=self._state_loop, daemon=True, name="pn-state").start()
        for srv in servers[1:]:
            threading.Thread(target=self._accept_loop, args=(srv,), daemon=True, name="pn-accept").start()
        self._accept_loop(servers[0])

    def _accept_loop(self, srv: socket.socket) -> None:
        # accept() itself can fail (a client that connects and resets at once
        # gives ECONNABORTED). That must never end the loop: a worker that has
        # stopped listening looks alive and can't be reached.
        while True:
            try:
                s, _ = srv.accept()
                threading.Thread(target=self._session, args=(s,), daemon=True, name="pn-session").start()
            except OSError:
                if srv.fileno() < 0:
                    return
                time.sleep(0.05)
            except Exception:  # noqa: BLE001
                traceback.print_exc(file=sys.__stderr__)
                time.sleep(0.05)

    def _state_loop(self) -> None:
        last = None
        while True:
            time.sleep(2.0)
            self._sweep()
            st = self.state()
            key = (st["thermal"], st["lifecycle"], st["low_power"])
            if key != last:
                last = key
                self._send({"t": "state", **st})
            avail = st.get("avail_mem")
            if avail:  # keep the cache well inside what jetsam allows
                self.store.set_budget(int((avail + self.store.size) * 0.35))

    def _admit(self, msg: dict) -> str | None:
        """Several Mac sessions can share the phone, as long as they share one
        Python environment: the import hook can mirror only one at a time."""
        env = _env_key(msg.get("manifest"))
        with self._lock:
            busy = [s for s in self.sessions.values() if s.live and s.id != msg.get("session")]
            if env is not None and any(s.env is not None and s.env != env for s in busy):
                return ("the phone is serving another pocketnode session from a different Python "
                        "environment; close that one (or use the same environment) and it will connect")
        return None

    def _handshake(self, conn: Connection) -> dict:
        return server_handshake(conn, self.token, self.hello(), admit=self._admit, after_auth=self._after_auth)

    def _after_auth(self) -> dict:
        """Told only to a Mac that proved its pairing: the inputs already on this node's disk,
        so they aren't sent again after an app restart."""
        return {"cached": self.store.cached()}

    def _session_started(self, sess: _Session, conn: Connection, info: dict) -> None:
        """Hook: a Mac session has authenticated on `conn` (its peer is conn.sock)."""

    def _sweep(self, keep: str | None = None) -> None:
        """Forget sessions gone for more than STALE seconds, and what they left here."""
        now = time.monotonic()
        with self._lock:
            gone = {k for k, s in self.sessions.items() if not s.live and k != keep and now - s.seen > STALE}
            for k in gone:
                del self.sessions[k]
            keys = [k for k, e in self.kept.items() if e["sid"] in gone]
            for k in [k for k, o in self.objects.items() if o["sid"] in gone]:
                del self.objects[k]
        for k in keys:
            self._unkeep(k)

    def _session(self, sock: socket.socket) -> None:
        conn = Connection(sock, "mac")
        try:
            info = self._handshake(conn)
        except Exception:
            conn.close()
            return
        sid = str(info.get("session") or uuid.uuid4().hex)
        env = _env_key(info.get("manifest"))
        self._sweep(keep=sid)
        with self._lock:
            now = time.monotonic()
            sess = self.sessions.setdefault(sid, _Session(sid))
            old, sess.conn, sess.env, sess.seen = sess.conn, conn, env, now
            new_env = env is not None and env != self.env
            if new_env:
                self.env = env
        if old is not None:
            old.close()
        self._session_started(sess, conn, info)
        if new_env:
            self.finder.set_manifest(info["manifest"])
        self._flush_held(sess)
        try:
            while True:
                msg, bufs = conn.recv()
                self._dispatch(sess, msg, bufs)
        except ConnectionClosed:
            pass
        finally:
            with self._lock:
                if sess.conn is conn:
                    sess.conn = None
                sess.seen = time.monotonic()
            conn.close()

    def _send(self, header: dict, bufs=(), sid: str | None = None) -> bool:
        """Queue a message for one Mac session (or all of them when sid is None).

        Only the writer thread touches the sockets, so a reader thread never
        blocks on a full send buffer (which can deadlock)."""
        with self._lock:
            targets = [s for s in self.sessions.values() if s.live and (sid is None or s.id == sid)]
        for s in targets:
            self._outq.put((s, s.conn, header, list(bufs)))
        return bool(targets)

    def _deliver(self, header: dict, bufs, sid: str | None) -> None:
        """Send a result, or hold it for when its session reconnects."""
        if self._send(header, bufs, sid) or sid is None:
            return
        with self._lock:
            self.sessions.setdefault(sid, _Session(sid)).held[header["id"]] = (header, list(bufs))

    def _writer(self) -> None:
        while True:
            sess, conn, header, bufs = self._outq.get()
            try:
                conn.send(header, bufs)
            except ConnectionClosed:
                if header.get("t") != "result":
                    continue
                with self._lock:
                    cur = sess.conn
                    if cur is None or cur is conn or cur.closed:
                        sess.held[header["id"]] = (header, bufs)
                        continue
                self._outq.put((sess, cur, header, bufs))  # the session reconnected: deliver there
            except Exception:  # noqa: BLE001 - one bad message must not stop the writer
                traceback.print_exc(file=sys.__stderr__)

    def _flush_held(self, sess: _Session) -> None:
        with self._lock:
            held, sess.held = sess.held, {}
        for h, b in held.values():
            self._deliver(h, b, sess.id)

    # ------------------------------------------------------------ messages

    def _dispatch(self, sess: _Session, msg: dict, bufs: list[bytearray]) -> None:
        t = msg["t"]
        sid = sess.id
        if t in ("task", "create", "call"):
            msg["_sid"] = sid
            self._accept_task(msg, bufs)
        elif t == "drop":
            with self._lock:
                self.objects.pop(str(msg.get("obj")), None)
        elif t == "ack":
            with self._ack_cv:
                a = self._acks.get(str(msg.get("id")))
                if a is not None:
                    a[0] = max(a[0], int(msg.get("n", 0)))
                    self._ack_cv.notify_all()
        elif t == "put":
            self._on_put(msg["hashes"], bufs)
        elif t == "module":
            ev = self._pending_imports.get(msg["req"])
            if ev:
                ev[1].extend([msg, bufs[0] if bufs else None])
                ev[0].set()
        elif t == "cancel":
            self._cancel(msg["id"])
        elif t == "pin":
            ok = [h for h in msg["hashes"] if self.store.pin(h)]
            self._send({"t": "pinned", "req": msg.get("req"), "hashes": ok,
                        "missing": [h for h in msg["hashes"] if h not in ok]}, sid=sid)
        elif t == "unpin":
            for h in msg["hashes"]:
                self.store.unpin(h)
        elif t == "keep":
            self._on_keep(sid, msg, bufs)
        elif t == "unkeep":
            self._unkeep(str(msg.get("key")))
        elif t == "inventory":  # diagnostics: what this session has kept here
            with self._lock:
                kept = [k for k, e in self.kept.items() if e["sid"] == sid]
                objects = [k for k, o in self.objects.items() if o["sid"] == sid]
            self._send({"t": "inventory", "req": msg.get("req"), "kept": kept, "objects": objects}, sid=sid)
        elif t == "ping":
            self._send({"t": "pong", "ts": msg.get("ts"), "state": self.state(), "store": self.store.stats()},
                       sid=sid)
        elif t == "override_state":
            self.override = msg.get("state", {})
            if "lifecycle" in self.override:
                self.lifecycle = self.override["lifecycle"]
            elif self.lifecycle not in ("active", "terminating") and not device.IS_IOS:
                self.lifecycle = "active"
            self._send({"t": "state", **self.state()})
        elif t == "drop_cache":
            self.store.clear()
        elif t == "stacks":  # diagnostics: what every thread is doing right now
            frames = sys._current_frames()
            names = {th.ident: th.name for th in threading.enumerate()}
            text = "\n".join(f"--- {names.get(i, i)}\n" + "".join(traceback.format_stack(f)[-12:])
                             for i, f in frames.items())
            self._send({"t": "stacks", "req": msg.get("req"), "text": text[-60000:]}, sid=sid)
        elif t == "echo":  # throughput probe
            self._send({"t": "echo", "n": sum(len(b) for b in bufs)}, bufs if msg.get("back") else (), sid=sid)
        elif t == "install":
            self._on_install(sid, msg, bufs)
        elif t == "uninstall":
            threading.Thread(target=self._uninstall, args=(sid, msg), daemon=True, name="pn-install").start()
        elif t == "restart":
            def bye():
                time.sleep(0.5)  # let queued replies go out
                os._exit(0)
            threading.Thread(target=bye, daemon=True, name="pn-restart").start()

    # ------------------------------------------------- packages added live

    def _on_install(self, sid: str, msg: dict, bufs: list[bytearray]) -> None:
        """One batch of an added package's files. They land in an incoming
        directory; the last batch moves them into the site directory."""
        req = str(msg["req"])
        inc = self.site.parent / f"site.incoming-{req}"
        try:
            if msg.get("seq", 0) == 0:
                shutil.rmtree(inc, ignore_errors=True)
                inc.mkdir(parents=True)
                self._installs[req] = {m.split(".")[0] for m in list(sys.modules)}
            elif self._installs.get(req, False) is None:  # an earlier batch failed and said so
                if msg.get("last"):
                    self._installs.pop(req, None)
                return
            elif req not in self._installs:
                raise RuntimeError("files arrived for an install that never started")
            files = msg.get("files", [])
            if len(files) != len(bufs):
                raise ValueError(f"{len(files)} files listed but {len(bufs)} sent")
            for f, b in zip(files, bufs):
                rel = Path(f["p"])
                if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                    raise ValueError(f"refusing to write {f['p']!r} outside the site directory")
                dst = inc / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                with open(dst, "wb") as fh:
                    fh.write(b)
                os.chmod(dst, 0o755 if f.get("x") else 0o644)
            if msg.get("last"):
                before = self._installs.pop(req)
                # Not a task slot: those may all be busy for a long time.
                threading.Thread(target=self._finish_install, args=(sid, req, inc, before),
                                 daemon=True, name="pn-install").start()
        except Exception as e:  # noqa: BLE001 - must not take down the session's reader
            self._installs[req] = None
            if msg.get("last"):
                self._installs.pop(req, None)
            shutil.rmtree(inc, ignore_errors=True)
            self._send({"t": "installed", "req": req,
                        "error": f"writing the files on {self.name} failed: {type(e).__name__}: {e}"}, sid=sid)

    def _finish_install(self, sid: str, req: str, inc: Path, before: set[str]) -> None:
        try:
            names, tops = set(), set()
            for e in sorted(inc.iterdir()):
                dst = self.site / e.name
                tops.add(e.name.split(".")[0])
                if e.name == SHIMS and e.is_dir() and dst.is_dir():
                    for c in e.iterdir():
                        _remove(dst / c.name)
                        os.replace(c, dst / c.name)
                    continue
                _remove(dst)
                os.replace(e, dst)
                top = _importable(dst)
                if top:
                    names.add(top)
            shutil.rmtree(inc, ignore_errors=True)
            self._refresh_imports(tops)
            # A native library can't be swapped under a running process: modules
            # already loaded switch to the new version when the app restarts.
            restart = sorted(n for n in names if n in before)
            failed = {}
            for n in sorted(names - set(restart)):
                try:
                    importlib.import_module(n)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as e:  # noqa: BLE001 - reported to the Mac
                    failed[n] = f"{type(e).__name__}: {e}"[:300]
            reply = {"t": "installed", "req": req, "packages": self.packages(), "failed": failed, "restart": restart}
        except BaseException as e:  # noqa: BLE001
            shutil.rmtree(inc, ignore_errors=True)
            reply = {"t": "installed", "req": req,
                     "error": f"installing on {self.name} failed: {type(e).__name__}: {e}"}
        self._send(reply, sid=sid)

    def _uninstall(self, sid: str, msg: dict) -> None:
        import importlib.metadata as md
        req = msg.get("req")
        try:
            want = {_norm(n): n for n in msg.get("dists", ())}
            removed, bundled, tops = [], [], set()
            for d in md.distributions(path=[str(self.site)]):
                n = d.metadata["Name"]
                if not n or _norm(n) not in want:
                    continue
                entries = {f.parts[0] for f in d.files or () if f.parts}
                info = getattr(d, "_path", None)
                if info is not None:
                    entries.add(Path(info).name)
                for first in entries:
                    if first.startswith("..") or os.path.isabs(first) or first == SHIMS:
                        continue
                    _remove(self.site / first)
                    tops.add(first.split(".")[0])
                removed.append(n)
                want.pop(_norm(n), None)
            for key, n in want.items():
                try:
                    md.distribution(n)
                    bundled.append(n)
                except md.PackageNotFoundError:
                    pass
            loaded = {m.split(".")[0] for m in list(sys.modules)}
            restart = sorted(t for t in tops if t in loaded)
            self._refresh_imports(tops)
            reply = {"t": "installed", "req": req, "packages": self.packages(), "removed": removed,
                     "bundled": bundled, "restart": restart}
        except BaseException as e:  # noqa: BLE001
            reply = {"t": "installed", "req": req,
                     "error": f"removing on {self.name} failed: {type(e).__name__}: {e}"}
        self._send(reply, sid=sid)

    def _refresh_imports(self, tops: set[str]) -> None:
        importlib.invalidate_caches()
        f = self.finder
        with f._lock:
            for n in tops:
                f.resolved.pop(n, None)
                f.missing_compiled.pop(n, None)
            f.local_versions = _local_versions()

    def _accept_task(self, msg: dict, bufs: list[bytearray]) -> None:
        # bufs[0] is the pickle; then inline buffers for specs with "i": 1.
        tid = msg["id"]
        specs = msg.get("bufs", [])
        it = iter(bufs[1:])
        new = {}
        for s in specs:
            if s.get("i"):
                b = next(it)
                if digest(b) != s["h"]:
                    # The array changed on the Mac between hashing and sending.
                    # This task gets what was sent, as it would in a thread; the
                    # cache keeps nothing under the stale address.
                    self.store.forget(s["h"])
                    s["h"] = f"{s['h']}@{tid}"
                new[s["h"]] = b
        hashes = [s["h"] for s in specs]
        with self._lock:
            self.inflight.add(tid)
        missing = set(self.store.reserve(hashes, new))
        if missing:
            with self._lock:
                self._waiting_bufs[tid] = (msg, bufs[0], missing)
                self._reserved[tid] = [h for h in hashes if h not in missing]
            self._send({"t": "need", "id": tid, "hashes": sorted(missing)}, sid=msg.get("_sid"))
            return
        with self._lock:
            self._reserved[tid] = hashes
        self.pool.submit(self._run_safe, msg, bufs[0])

    def _on_put(self, hashes: list[str], bufs: list[bytearray]) -> None:
        good = {}
        for h, b in zip(hashes, bufs):
            if digest(b) == h:
                good[h] = b
                continue
            # Changed on the Mac since it was hashed: the waiting tasks get these
            # bytes privately and the cache keeps nothing under the stale address.
            self.store.forget(h)
            with self._lock:
                for tid, (msg, _, missing) in self._waiting_bufs.items():
                    if h in missing:
                        key = f"{h}@{tid}"
                        for s in msg.get("bufs", []):
                            if s["h"] == h:
                                s["h"] = key
                        missing.discard(h)
                        missing.add(key)
                        good[key] = b
        # Held until the waiting tasks have taken theirs: a buffer bigger than the
        # cache would otherwise be evicted as it arrives, and its tasks would wait forever.
        self.store.reserve(list(good), good)
        ready = []
        try:
            with self._lock:
                for tid, (msg, payload, missing) in list(self._waiting_bufs.items()):
                    got = [h for h in missing if h in good]
                    if not got:
                        continue
                    still = set(self.store.reserve(got))
                    self._reserved.setdefault(tid, []).extend(h for h in got if h not in still)
                    missing.difference_update(set(got) - still)
                    if not missing:
                        del self._waiting_bufs[tid]
                        ready.append((msg, payload))
        finally:
            self.store.release(list(good))
        for msg, payload in ready:
            self.pool.submit(self._run_safe, msg, payload)

    # ------------------------------------------------------ pinned objects

    def _on_keep(self, sid: str, msg: dict, bufs: list[bytearray]) -> None:
        """A Mac pinned an object: hold its buffers, and build it when a task first uses it."""
        key = str(msg["key"])
        hashes = list(msg.get("hashes", ()))
        self._unkeep(key)  # sent again (some buffers were missing): replaces the old entry
        pinned = [h for h in hashes if self.store.pin(h)]
        with self._lock:
            self.kept[key] = {"data": bufs[0] if bufs else b"", "hashes": hashes, "pinned": pinned, "sid": sid,
                              "label": str(msg.get("label") or "object"), "lock": threading.Lock()}
        if len(pinned) < len(hashes):  # its file was gone: the Mac sends those again
            self._send({"t": "pinned", "key": key, "hashes": pinned,
                        "missing": [h for h in hashes if h not in pinned]}, sid=sid)

    def _unkeep(self, key: str) -> None:
        with self._lock:
            e = self.kept.pop(key, None)
        if e is None:
            return
        for h in e["pinned"]:
            self.store.unpin(h)
        if e.get("held"):
            self.store.release(e["held"])

    def _kept_object(self, key: str):
        """A task refers to a pinned object: the copy kept here (built the first time)."""
        with self._lock:
            e = self.kept.get(key)
        if e is None:
            raise Unavailable("pin", f"an object pinned on the Mac isn't kept on {self.name}")
        with e["lock"]:
            if "obj" not in e:
                bufs = self.store.acquire(e["hashes"])
                if bufs is None:
                    self._unkeep(key)
                    raise Unavailable("pin", f"the pinned {e['label']}'s data isn't on {self.name} any more")
                e["held"] = e["hashes"]
                # Shared by every task here and backed by the cache's own bytes: read-only.
                e["obj"] = serialize.loads(e["data"], [memoryview(b).toreadonly() for b in bufs])
                e["data"] = b""
        return e["obj"]

    # ---------------------------------------------------------- cancelling

    def _cancel(self, tid: str) -> None:
        """Stop a task the Mac no longer wants (a speculative copy that lost).

        User code running in the main interpreter is interrupted with an
        asynchronous exception. It is only ever posted while the task's thread
        is registered in `running`, under the lock that _unregister takes, so
        it can't land in a later task or in the thread pool's own loop. A task in
        a subinterpreter is interrupted there (see subinterp._RUNNER).
        """
        with self._lock:
            if tid not in self.inflight or tid in self.cancelled:
                return
            self.cancelled.add(tid)
            waiting = self._waiting_bufs.pop(tid, None)
            ident = self.running.get(tid)
            if ident:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident), ctypes.py_object(TaskCancelled))
            sub_cancel = self._sub_cancels.get(tid)
            if sub_cancel is not None:
                sub_cancel()  # only writes a byte to a pipe
        if waiting is not None:
            self._cleanup(tid)

    def _unregister(self, tid: str) -> None:
        """Leave the interruptible region; any cancel not yet delivered is withdrawn."""
        ident = threading.get_ident()
        while True:
            try:
                with self._lock:
                    self.running.pop(tid, None)
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident), None)
                return
            except TaskCancelled:
                continue

    def _cleanup(self, tid: str) -> bool:
        """Release everything a task held. Idempotent. Returns whether it was cancelled."""
        self._unregister(tid)
        with self._lock:
            self.inflight.discard(tid)
            cancelled = tid in self.cancelled
            self.cancelled.discard(tid)
            self._sub_cancels.pop(tid, None)
            hashes = self._reserved.pop(tid, None)
        if hashes:
            self.store.release(hashes)
        return cancelled

    def _run_safe(self, msg: dict, payload: bytearray) -> None:
        # The thread pool swallows exceptions; a lost result would hang the
        # Mac's future forever, so anything unexpected becomes an infra error.
        tid = msg["id"]
        result = None
        try:
            result = self._run(msg, payload)
        except TaskCancelled:
            pass
        except BaseException as exc:  # noqa: BLE001
            result = ({"t": "result", "id": tid, "node": self.id, "ok": False, "infra": True,
                       "error": _format_exc(exc), "exc_type": type(exc).__name__}, [])
        cancelled = self._cleanup(tid)
        if msg.get("t") == "create" and (result is None or cancelled or not result[0].get("ok")):
            with self._lock:  # built, then refused (a guard tripped late) or cancelled: nobody holds it
                self.objects.pop(str(msg.get("obj")), None)
        if result is None or cancelled:
            return
        header, out_bufs = result
        header["evicted"] = self.store.take_evicted()
        header["state"] = self.state()
        self.tasks_done += 1
        self._deliver(header, out_bufs, msg.get("_sid"))

    # ----------------------------------------------------------- execution

    def _run(self, msg: dict, payload: bytearray) -> tuple[dict, list] | None:
        tid = msg["id"]
        with self._lock:
            if tid in self.cancelled:
                return None
        self._tl.sid = msg.get("_sid")
        hashes = [s["h"] for s in msg.get("bufs", [])]
        t0 = time.perf_counter()
        header = {"t": "result", "id": tid, "node": self.id}
        out_bufs: list = []
        try:
            held = self.store.get(hashes)
            # Each task gets its own writable copies unless the caller allowed
            # sharing read-only views (joblib passes read-only memmaps anyway).
            ro = msg.get("ro")
            copied = sum(len(b) for b in held if ro is None or len(b) < ro)
            avail = device.available_memory()
            if avail and copied > avail - (256 << 20):
                raise Unavailable("memory", f"its inputs need {copied / 2**30:.1f} GB more memory and "
                                            f"{self.name} has {avail / 2**30:.1f} GB free")
            views = [memoryview(b).toreadonly() if ro is not None and len(b) >= ro else bytearray(b)
                     for b in held]
            t1 = time.perf_counter()
            fk = msg.get("fk")
            if self.subpool is not None and msg.get("pure") and self.no_sub.get(fk, 0) < 2:
                done = self._run_sub(msg, payload, views, header, t0, t1)
                if done is not None:
                    return header, done
            fn, args, kwargs = self._callable(msg, payload, views)
            t2 = time.perf_counter()
            guards.begin_task()
            with _captured() as (o, e):
                try:
                    try:
                        with self._lock:
                            if tid in self.cancelled:
                                raise TaskCancelled
                            self.running[tid] = threading.get_ident()
                        value = fn(*args, **kwargs)
                        raised = None
                    finally:
                        self._unregister(tid)
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
        except TaskCancelled:
            raise
        except Unavailable as u:
            header.update(ok=False, unavailable={"kind": u.kind, "detail": u.detail, "module": u.module})
        except BaseException as exc:  # failure to load the task itself
            header.update(ok=False, error=_format_exc(exc), exc_type=type(exc).__name__, infra=True)
            missing = self._missing_from(exc)
            if missing:
                header["unavailable"] = {"kind": missing.kind, "detail": missing.detail, "module": missing.module}
        return header, out_bufs

    def _callable(self, msg: dict, payload, views) -> tuple:
        """(fn, args, kwargs) for a message: a task's own, or building / calling a kept object."""
        kept = self._kept_object if msg.get("pins") else None
        t = msg.get("t", "task")
        stream = bool(msg.get("stream"))
        if t == "task":
            fn, args, kwargs = serialize.loads(payload, views, kept=kept)
            if stream:
                return (lambda *a, **k: self._stream(msg, fn(*a, **k))), args, kwargs
            return fn, args, kwargs
        oid, label = str(msg.get("obj")), str(msg.get("label") or "object")
        if t == "create":
            factory, args, kwargs = serialize.loads(payload, views, kept=kept)
            sid = msg.get("_sid")

            def create(*a, **k):
                value = factory(*a, **k)
                with self._lock:
                    self.objects[oid] = {"value": value, "sid": sid, "label": label, "lock": threading.Lock()}

            return create, args, kwargs
        args, kwargs = serialize.loads(payload, views, kept=kept)
        with self._lock:
            o = self.objects.get(oid)
        if o is None:
            raise Unavailable("object", f"{label.rsplit('.', 1)[0]} isn't on {self.name} (the app restarted, "
                                        "or it was dropped)")
        name = str(msg["method"])

        def call(*a, **k):
            lock = o["lock"]
            while not lock.acquire(timeout=0.05):  # (a timeout, so a cancel can land while it waits)
                pass
            try:
                r = getattr(o["value"], name)(*a, **k)
                return self._stream(msg, r) if stream else r
            finally:
                lock.release()

        return call, args, kwargs

    def _stream(self, msg: dict, it) -> int:
        """Send what a streaming task yields, item by item, staying at most `window` items
        ahead of what the Mac has taken. Returns how many were sent."""
        if not hasattr(it, "__next__"):
            raise TypeError(f"{msg.get('label') or 'the function'} returned {type(it).__name__}, not an iterator: "
                            ".stream() needs a generator (a function that yields) or another iterator")
        tid, sid = str(msg["id"]), msg.get("_sid")
        window = max(1, int(msg.get("window") or 32))
        taken = [0]
        with self._ack_cv:
            self._acks[tid] = taken
        n = 0
        try:
            for item in it:
                p = serialize.dumps_plain(item)
                # Copies of its buffers: the generator may reuse an array for its next item.
                if not self._send({"t": "item", "id": tid, "n": n}, [p.data, *(bytes(b) for b in p.buffers)],
                                  sid=sid):
                    raise TaskCancelled  # the Mac that asked is gone; nobody will read the rest
                n += 1
                with self._ack_cv:
                    while n - taken[0] >= window:
                        if not self._live(sid):
                            raise TaskCancelled
                        self._ack_cv.wait(0.1)
        finally:
            with self._ack_cv:
                self._acks.pop(tid, None)
            close = getattr(it, "close", None)
            if close is not None:
                close()  # a generator stopped early runs its finally blocks now
        return n

    def _live(self, sid) -> bool:
        with self._lock:
            s = self.sessions.get(sid)
            return s is not None and s.live

    def _prefetch(self, mods) -> None:
        """Fetch pure-Python modules a task names, so subinterpreters (which have
        no import hook of their own) find them on disk."""
        import importlib.util
        for m in mods:
            if m in self._prefetched:
                continue
            self._prefetched.add(m)
            try:
                importlib.util.find_spec(m)
            except Exception:  # noqa: BLE001 - the task's own import reports it
                pass

    def _run_sub(self, msg, payload, views, header, t0, t1) -> list | None:
        """Run a pure task in a subinterpreter. None means: run it in the main interpreter."""
        fk, tid = msg.get("fk"), msg["id"]

        def on_cancel(cancel):
            with self._lock:
                self._sub_cancels[tid] = cancel
                if tid in self.cancelled:
                    cancel()

        try:
            self._prefetch(msg.get("mods", ()))
            out = self.subpool.run(payload, views, on_cancel=on_cancel)
        except Exception as e:  # noqa: BLE001 - infrastructure trouble: fall back
            out = {"status": "needs-main", "detail": repr(e)}
        finally:
            with self._lock:
                self._sub_cancels.pop(tid, None)
        t3 = time.perf_counter()
        status = out["status"]
        if status == "cancelled":
            raise TaskCancelled
        if status == "needs-main":
            # Usually a module the main interpreter fetches on first import; after
            # that the subinterpreter finds it too. Twice means it can't run there.
            self.no_sub[fk] = self.no_sub.get(fk, 0) + 1
            self.sub_fallbacks[str(fk)[-60:]] = str(out.get("detail"))[:300]
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
            # Ask the session whose task is importing; any session will do
            # otherwise, since they all share one environment.
            sid = getattr(self._tl, "sid", None)
            if not self._send({"t": "import", "req": req, "name": name, "have": have}, sid=sid):
                with self._lock:
                    live = next((s.id for s in self.sessions.values() if s.live), None)
                if live is None or not self._send({"t": "import", "req": req, "name": name, "have": have},
                                                  sid=live):
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


def _remove(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    elif p.exists() or p.is_symlink():
        p.unlink()


def _importable(p: Path) -> str | None:
    """The top-level module name a site-directory entry provides, if any."""
    if p.name == SHIMS or p.name.endswith((".dist-info", ".data")):
        return None
    if p.is_dir():
        if (p / "__init__.py").exists() or any(p.rglob("*.so")):
            return p.name
        return None
    if p.name.endswith((".py", ".so")):
        return p.name.split(".")[0]
    return None


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


def main(argv=None, standin: bool = False) -> None:
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
    ap.add_argument("--store-budget", type=int, help="bytes of input data to keep cached")
    ap.add_argument("--site-dir", help="where packages added at runtime go (default: under the cache dir)")
    ap.add_argument("--edition", choices=["dev", "store"], default="dev",
                    help="store: behave like the App Store edition (pairing at run time, fixed packages)")
    ap.add_argument("--pair-file", help="with --edition store: the file holding the paired Macs")
    a = ap.parse_args(argv)
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1, file=sys.__stderr__, all_threads=True)  # `kill -USR1` dumps stacks
    kw = dict(slots=a.slots, cache_dir=a.cache_dir, phone=a.phone or None, name=a.name, node_kind=a.kind,
              store_budget=a.store_budget, site_dir=a.site_dir)
    if a.edition == "store":
        if not a.pair_file:
            ap.error("--edition store needs --pair-file")
        from .pairing import FileBackend, PairStore
        from .storeworker import StoreWorker
        w = StoreWorker(PairStore(FileBackend(a.pair_file)), **kw)
        code = os.environ.get("POCKETNODE_STANDIN_PAIR_CODE", "")
        if standin and code and not device.IS_IOS:
            # A stand-in has no screen: this opens a pairing window that stays
            # open and answers Allow by itself (for trying `pocketnode pair`).
            w.window.auto_allow = True
            w.window.begin(code=code, keep_open=True)
    else:
        w = Worker(_read_token(a.token_file), **kw)
    w.serve(listen=a.listen, unix=a.unix, connect=a.connect,
            ready=lambda addr: print(json.dumps({"ready": str(addr)}), file=sys.__stdout__, flush=True))


if __name__ == "__main__":
    main(standin=True)
