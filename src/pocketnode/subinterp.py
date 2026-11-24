"""Pure-Python tasks on every core: one subinterpreter (with its own GIL) per slot.

iOS has no child processes and no free-threaded CPython build, so threads in
one interpreter run Python bytecode one at a time. Python 3.13's isolated
subinterpreters each have their own GIL, which is the platform's way to run
pure-Python code in parallel. Tasks that only touch the stdlib and pure-Python
packages run here; anything else (numpy, which doesn't support subinterpreters,
or any import that fails here) runs in the main interpreter instead.
"""

from __future__ import annotations

import os
import pickle
import queue
import struct
import sys
import threading

try:
    import _interpreters
except ImportError:  # pragma: no cover - not built
    _interpreters = None

AVAILABLE = _interpreters is not None

# Runs inside the subinterpreter. Inputs arrive as __main__ attributes; the
# outcome goes back as one length-prefixed pickle on the pipe `fd`.
#
# Cancelling: a byte on `cancel_fd` (written by SubPool.run's cancel()) makes a
# watcher thread raise Cancelled in the user's call. PyThreadState_SetAsyncExc
# only reaches threads of the calling interpreter, so the watcher lives here.
# Isolated subinterpreters refuse daemon threads, and a thread left blocked here
# would hang interpreter shutdown, so the runner wakes it with a byte on
# `wake_fd` and joins it before returning. Both fds belong to SubPool.run and
# stay open until exec returns, so no write ever finds the read end closed
# (iOS doesn't ignore SIGPIPE).
_RUNNER = r"""
import os, sys, io, pickle, struct, threading, traceback
for p in extra_paths.split("\0"):
    if p and p not in sys.path:
        sys.path.append(p)
import builtins
builtins._pn_ext_fail = None

class Cancelled(BaseException):
    pass

def _pn_watch(fd, st, lk, interrupt):
    if os.read(fd, 1):
        with lk:
            if st[0] == "running":
                interrupt(Cancelled)
            elif st[0] == "pending":
                st[0] = "cancelled"

def _pn_finish(st, lk, interrupt):
    # Withdraw a cancel that hasn't landed yet, so it can't escape into the
    # code that sends the result.
    while True:
        try:
            with lk:
                st[0] = "done"
                if interrupt is not None:
                    interrupt(None)
            return
        except Cancelled:
            continue

st, lk, interrupt, watcher = ["pending"], threading.Lock(), None, None
if cancel_fd >= 0:
    try:
        import ctypes
        _pn_set = ctypes.pythonapi.PyThreadState_SetAsyncExc
        _pn_me = ctypes.c_ulong(threading.get_ident())
        def interrupt(exc, _set=_pn_set, _me=_pn_me, _obj=ctypes.py_object):
            _set(_me, None if exc is None else _obj(exc))
        t = threading.Thread(target=_pn_watch, args=(cancel_fd, st, lk, interrupt), name="pn-cancel")
        t.start()
        watcher = t
    except Exception:
        interrupt = None  # no ctypes here: the task just runs to the end
out = {}
o, e = io.StringIO(), io.StringIO()
so, se = sys.stdout, sys.stderr
sys.stdout, sys.stderr = o, e
try:
    from pocketnode import guards
    guards.begin_task()
    try:
        import cloudpickle
        fn, args, kwargs = pickle.loads(payload, buffers=[globals()[f"b{i}"] for i in range(nbufs)])
        try:
            with lk:
                if st[0] == "cancelled":
                    raise Cancelled
                st[0] = "running"
            value = fn(*args, **kwargs)
        finally:
            _pn_finish(st, lk, interrupt)
        out["value"] = cloudpickle.dumps(value, protocol=5)
        out["status"] = "ok"
    except Cancelled:
        out["status"] = "cancelled"
    except guards._Escape as esc:
        out["status"] = "unavailable"
        out["u"] = (esc.err.kind, esc.err.detail, esc.err.module)
    except ImportError as exc:
        # Can't load here (an extension without subinterpreter support, or a
        # module the main interpreter fetches on demand): run it there instead.
        out["status"] = "needs-main"
        out["detail"] = str(exc)
    except BaseException as exc:
        out["status"] = "error"
        out["tb"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        out["etype"] = type(exc).__name__
        try:
            import cloudpickle
            out["exc"] = cloudpickle.dumps(exc, protocol=5)
        except Exception:
            pass
    trip = guards.end_task()
    if trip is not None:
        out["status"] = "unavailable"
        out["u"] = (trip.kind, trip.detail, trip.module)
    if getattr(builtins, "_pn_ext_fail", None):
        out = {"status": "needs-main", "detail": f"{builtins._pn_ext_fail} can't load in a subinterpreter"}
except BaseException as exc:
    out = {"status": "needs-main", "detail": repr(exc)}
finally:
    sys.stdout, sys.stderr = so, se
    if watcher is not None:
        os.write(wake_fd, b"\0")
        watcher.join()
try:
    out["stdout"], out["stderr"] = o.getvalue()[-200000:], e.getvalue()[-200000:]
    data = pickle.dumps(out, protocol=5)
    v = memoryview(struct.pack("<Q", len(data)) + data)
    while v:
        v = v[os.write(fd, v):]
finally:
    os.close(fd)
"""

_SETUP = r"""
import sys, os
sys.path[:] = [p for p in paths.split("\0") if p]
sys.setrecursionlimit(recursion)
from pocketnode import guards
if roots:
    guards.install(roots.split("\0"), scratch, tuple(p for p in phone_dirs.split("\0") if p))
# An extension module that can't load here must send the task to the main
# interpreter even when the importing package swallows the ImportError and
# carries on with a fallback.
import builtins, importlib.machinery as _mach
builtins._pn_ext_fail = None
def _watch(cls):
    real_create, real_exec = cls.create_module, cls.exec_module
    def flag(name):
        # The stdlib's own optional accelerators have equivalent pure-Python
        # fallbacks; a third-party package's fallback may not be equivalent.
        if name.partition(".")[0] not in sys.stdlib_module_names:
            builtins._pn_ext_fail = name
    def create_module(self, spec):
        # Third-party extension modules never load here. Most don't support
        # subinterpreters, and not all refuse cleanly: numpy's attempt leaves
        # process-wide state behind that breaks numpy in the main interpreter
        # too ("CPU dispatcher tracer already initialized").
        if spec.name.partition(".")[0] not in sys.stdlib_module_names:
            flag(spec.name)
            raise ImportError(f"{spec.name} is an extension module; it runs in the main interpreter",
                              name=spec.name)
        return real_create(self, spec)
    def exec_module(self, module):
        try:
            return real_exec(self, module)
        except BaseException:
            flag(module.__name__)
            raise
    cls.create_module = create_module
    cls.exec_module = exec_module
for _n in ("ExtensionFileLoader", "AppleFrameworkLoader"):
    if hasattr(_mach, _n):
        _watch(getattr(_mach, _n))
"""


def _restore_gilstate_check() -> None:
    """Creating an interpreter makes PyGILState_Check() answer "held" forever,
    which breaks extension modules that assert it isn't (PyTorch autograd).
    The iOS host exports a function that turns the real check back on; that is
    sound here because subinterpreter work never shares a thread with anything else."""
    try:
        import ctypes
        ctypes.CDLL(None).pocketnode_enable_gilstate_check()
    except (AttributeError, OSError):
        pass  # not the iOS host (the Mac stand-in): nothing to restore


class SubPool:
    """One thread per subinterpreter, and those threads never run anything else.

    CPython's PyGILState API (which extension modules such as PyTorch use to
    check and take the GIL) keeps one thread state per OS thread and doesn't
    understand subinterpreters. A thread that has run code in a subinterpreter
    and later runs an extension module in the main interpreter can be told it
    holds the wrong GIL. So subinterpreter work stays on these threads, and the
    worker's main-interpreter task threads never enter a subinterpreter.
    """

    def __init__(self, n: int, roots: list[str] | None, scratch: str, phone_dirs: tuple[str, ...],
                 extra_paths=lambda: []):
        self.n = n
        self.roots, self.scratch, self.phone_dirs = roots, scratch, phone_dirs
        self.extra_paths = extra_paths
        self._jobs: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._idle = 0
        self._lock = threading.Lock()
        self.broken: str | None = None
        # Interpreters take a moment to set up; have them ready before the first task.
        threading.Thread(target=self._prewarm, daemon=True, name="pn-sub-warm").start()

    def _prewarm(self) -> None:
        with self._lock:
            while len(self._threads) < self.n:
                self._spawn()

    def _spawn(self) -> None:
        t = threading.Thread(target=self._serve, daemon=True, name=f"pn-sub-{len(self._threads)}")
        self._threads.append(t)
        t.start()

    def _serve(self) -> None:
        interp = None
        while True:
            with self._lock:
                self._idle += 1
            job = self._jobs.get()
            with self._lock:
                self._idle -= 1
            shared, done = job
            try:
                if interp is None:
                    interp = self._create()
                done.append(self._exec(interp, shared))
            except BaseException as e:  # noqa: BLE001 - reported to the caller, which falls back
                self.broken = repr(e)
                done.append({"status": "needs-main", "detail": f"subinterpreter failed: {e!r}"})
            finally:
                shared["_event"].set()

    def _create(self) -> int:
        interp = _interpreters.create(_interpreters.new_config("isolated"))
        err = _interpreters.exec(interp, _SETUP, shared={
            "paths": "\0".join(sys.path), "recursion": sys.getrecursionlimit(),
            "roots": "\0".join(self.roots or []), "scratch": self.scratch,
            "phone_dirs": "\0".join(self.phone_dirs)})
        if err is not None:
            _interpreters.destroy(interp)
            raise RuntimeError(f"subinterpreter setup failed: {err}")
        _restore_gilstate_check()
        return interp

    def _exec(self, interp: int, shared: dict) -> dict:
        chunks: list[bytes] = []
        r, w = os.pipe()
        reader = threading.Thread(target=lambda: chunks.extend(iter(lambda: os.read(r, 1 << 20), b"")),
                                  daemon=True)
        reader.start()
        try:
            args = {k: v for k, v in shared.items() if not k.startswith("_")}
            args["fd"] = w
            try:
                err = _interpreters.exec(interp, _RUNNER, shared=args)
            except BaseException:
                # exec raises only before the runner starts, so the runner never
                # got to close the write end.
                os.close(w)
                raise
        finally:
            reader.join()
            os.close(r)
        data = b"".join(chunks)
        if err is not None or len(data) < 8:
            return {"status": "needs-main", "detail": f"subinterpreter failed: {err}"}
        (n,) = struct.unpack_from("<Q", data)
        return pickle.loads(data[8:8 + n])

    def run(self, payload: bytes, buffers: list, on_cancel=None) -> dict:
        """Run one pickled (fn, args, kwargs) in a subinterpreter; returns the outcome dict.

        `on_cancel(cancel)` receives a function that interrupts the user's call
        (outcome status "cancelled"). It may be called at any time, any number
        of times, also after the run has finished."""
        r, w = os.pipe()
        lock = threading.Lock()
        sent = closed = False

        def cancel():
            nonlocal sent
            with lock:
                if not (sent or closed):
                    sent = True
                    os.write(w, b"\1")

        def close():
            nonlocal closed
            with lock:
                closed = True
                os.close(w)
                os.close(r)

        try:
            shared = {"payload": bytes(payload), "nbufs": len(buffers),
                      "extra_paths": "\0".join(self.extra_paths()), "cancel_fd": r, "wake_fd": w,
                      "_event": threading.Event()}
            for i, b in enumerate(buffers):
                shared[f"b{i}"] = bytes(b)
            done: list = []
            if on_cancel is not None:
                on_cancel(cancel)
            with self._lock:
                if self._idle == 0 and len(self._threads) < self.n:
                    self._spawn()
            self._jobs.put((shared, done))
        except BaseException:
            close()
            raise
        shared["_event"].wait()
        # Only once the runner is done with both ends: closing earlier could
        # hand their numbers to another file while it still uses them.
        close()
        return done[0]
