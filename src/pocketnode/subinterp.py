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
_RUNNER = r"""
import os, sys, io, pickle, struct, traceback
for p in extra_paths.split("\0"):
    if p and p not in sys.path:
        sys.path.append(p)
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
        value = fn(*args, **kwargs)
        out["value"] = cloudpickle.dumps(value, protocol=5)
        out["status"] = "ok"
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
except BaseException as exc:
    out = {"status": "needs-main", "detail": repr(exc)}
finally:
    sys.stdout, sys.stderr = so, se
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
"""


class SubPool:
    def __init__(self, n: int, roots: list[str] | None, scratch: str, phone_dirs: tuple[str, ...],
                 extra_paths=lambda: []):
        self.n = n
        self.roots, self.scratch, self.phone_dirs = roots, scratch, phone_dirs
        self.extra_paths = extra_paths
        self._idle: queue.Queue = queue.Queue()
        self._created = 0
        self._lock = threading.Lock()
        self.broken: str | None = None

    def _get(self):
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._created < self.n:
                self._created += 1
                try:
                    return self._create()
                except Exception as e:  # noqa: BLE001
                    self._created -= 1
                    self.broken = repr(e)
                    raise
        return self._idle.get()

    def _create(self) -> int:
        interp = _interpreters.create(_interpreters.new_config("isolated"))
        err = _interpreters.exec(interp, _SETUP, shared={
            "paths": "\0".join(sys.path), "recursion": sys.getrecursionlimit(),
            "roots": "\0".join(self.roots or []), "scratch": self.scratch,
            "phone_dirs": "\0".join(self.phone_dirs)})
        if err is not None:
            _interpreters.destroy(interp)
            raise RuntimeError(f"subinterpreter setup failed: {err}")
        return interp

    def run(self, payload: bytes, buffers: list) -> dict:
        """Run one pickled (fn, args, kwargs) in a free subinterpreter; returns the outcome dict."""
        # Build everything that can fail before holding an interpreter or a pipe.
        shared = {"payload": bytes(payload), "nbufs": len(buffers),
                  "extra_paths": "\0".join(self.extra_paths())}
        for i, b in enumerate(buffers):
            shared[f"b{i}"] = bytes(b)
        chunks: list[bytes] = []
        interp = self._get()
        try:
            r, w = os.pipe()
            reader = threading.Thread(target=lambda: chunks.extend(iter(lambda: os.read(r, 1 << 20), b"")),
                                      daemon=True)
            reader.start()
            try:
                shared["fd"] = w
                err = _interpreters.exec(interp, _RUNNER, shared=shared)
            except BaseException:
                # exec raises only before the runner starts, so the runner never
                # got to close the write end.
                os.close(w)
                raise
            finally:
                reader.join()
                os.close(r)
        finally:
            self._idle.put(interp)
        data = b"".join(chunks)
        if err is not None or len(data) < 8:
            return {"status": "needs-main", "detail": f"subinterpreter failed: {err}"}
        (n,) = struct.unpack_from("<Q", data)
        return pickle.loads(data[8:8 + n])

    def close(self) -> None:
        while True:
            try:
                _interpreters.destroy(self._idle.get_nowait())
            except queue.Empty:
                return
            except Exception:  # noqa: BLE001
                pass
