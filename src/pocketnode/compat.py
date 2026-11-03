"""Small runtime fixes so packages built for macOS behave on iOS.

Installed by the worker only when it runs on iOS.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path

_installed = False


def install() -> None:
    global _installed
    if _installed or sys.platform != "ios":
        return
    _installed = True
    # iOS apps get no USER/LOGNAME, so getpass.getuser() (used by torch and
    # others for cache paths) raises. The app runs as the "mobile" user.
    os.environ.setdefault("USER", "mobile")
    os.environ.setdefault("LOGNAME", "mobile")
    _redirect_ctypes()
    _stub_multiprocessing()
    when_imported("joblib.parallel", _joblib_threads)


def _joblib_threads(mod) -> None:
    """joblib on iOS: thread-based n_jobs at the real core count.

    joblib disables itself (n_jobs -> 1) when process semaphores are missing,
    which on iOS would also turn off its thread backend. Threads work fine, and
    the default backend there is already "threading"; requests for processes
    still hit the guards and send the task to the Mac.
    """
    import joblib
    import joblib._parallel_backends as pb
    import multiprocessing

    # Processes can't exist here: joblib's default and "prefer processes" both mean threads.
    mod.DEFAULT_BACKEND = mod.DEFAULT_PROCESS_BACKEND = "threading"
    if pb.mp is None:
        pb.mp = mod.mp = multiprocessing
        n = lambda only_physical_cores=False: os.cpu_count() or 1  # noqa: E731
        mod.cpu_count = joblib.cpu_count = pb.cpu_count = n
        from multiprocessing.pool import ThreadPool
        pb.ThreadPool = ThreadPool


_hooks: dict[str, list] = {}


def when_imported(name: str, fn) -> None:
    """Run fn(module) right after `name` is first imported (or now, if it already is)."""
    if name in sys.modules:
        fn(sys.modules[name])
        return
    _hooks.setdefault(name, []).append(fn)
    if not any(isinstance(f, _PostImportFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _PostImportFinder())


class _PostImportFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _hooks:
            return None
        import importlib.util
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return spec
        loader = spec.loader
        real_exec = loader.exec_module

        def exec_module(module):
            real_exec(module)
            for fn in _hooks.pop(fullname, ()):
                fn(module)

        loader.exec_module = exec_module
        return spec


def _redirect_ctypes() -> None:
    """Route ctypes loads of relocated dylibs to their frameworks.

    Staging moves every bundled dylib (e.g. torch/lib/libtorch_global_deps.dylib)
    into Frameworks/ so it can be signed. Packages that ctypes-load those files
    by path, or that guess a `.so` suffix because platform.system() isn't
    "Darwin", get the framework binary instead.
    """
    bundle = Path(sys.prefix).parent
    pk = bundle / "app_packages"
    try:
        moved = json.loads((pk / "pocketnode_dylib_map.json").read_text())
    except OSError:
        return
    by_name = {}
    for rel, fw in moved.items():
        by_name.setdefault(Path(rel).name, fw)
        by_name.setdefault(Path(rel).with_suffix(".so").name, fw)

    def resolve(name):
        if not isinstance(name, (str, os.PathLike)):
            return name
        s = os.fspath(name)
        if not os.path.isabs(s) or os.path.exists(s):
            return name
        try:
            rel = str(Path(s).resolve().relative_to(pk.resolve()))
        except ValueError:
            rel = None
        fw = moved.get(rel) if rel else None
        if fw is None and rel:
            fw = moved.get(str(Path(rel).with_suffix(".dylib")))
        if fw is None:
            fw = by_name.get(Path(s).name)
        return str(bundle / fw) if fw else name

    real_init = ctypes.CDLL.__init__

    def __init__(self, name, *a, **k):
        real_init(self, resolve(name), *a, **k)

    ctypes.CDLL.__init__ = __init__


def _stub_multiprocessing() -> None:
    """Provide `_multiprocessing` for a single-process world.

    iOS CPython doesn't build it: there are no child processes. torch imports
    multiprocessing at startup, and multiprocessing's thread pools, locks and
    queues are built on SemLock. Within one process a semaphore is just a
    thread semaphore, so that's what SemLock becomes. Starting an actual
    process is still caught by the guards and runs on the Mac instead.
    """
    try:
        import _multiprocessing  # noqa: F401
        return
    except ImportError:
        pass
    import threading
    import types

    mod = types.ModuleType("_multiprocessing")
    mod.flags = {}

    def sem_unlink(name):
        pass

    class SemLock:
        SEM_VALUE_MAX = 2**31 - 1

        def __init__(self, kind, value, maxvalue, name, unlink):
            # name None: no OS-level semaphore exists, so nothing to register or unlink
            self.kind, self.maxvalue, self.name = kind, maxvalue, None
            self.handle = id(self)
            self._sem = threading.Semaphore(value)
            self._owner = None
            self._depth = 0

        def acquire(self, block=True, timeout=None):
            me = threading.get_ident()
            if self.kind == 0 and self._owner == me:  # RECURSIVE_MUTEX
                self._depth += 1
                return True
            ok = self._sem.acquire(block, timeout if block else None)
            if ok:
                self._owner, self._depth = me, 1
            return ok

        def release(self):
            if self.kind == 0:
                if self._owner != threading.get_ident():
                    raise AssertionError("attempt to release recursive lock not owned by thread")
                self._depth -= 1
                if self._depth:
                    return
            self._owner = None
            self._depth = 0
            self._sem.release()

        __enter__ = acquire

        def __exit__(self, *a):
            self.release()

        def _count(self):
            return self._depth if self._owner == threading.get_ident() else 0

        def _is_mine(self):
            return self._owner == threading.get_ident() and self._depth > 0

        def _get_value(self):
            return self._sem._value

        def _is_zero(self):
            return self._sem._value == 0

        def _after_fork(self):
            pass

        @classmethod
        def _rebuild(cls, *a):
            raise OSError("semaphores can't be shared with other processes on iOS")

    mod.sem_unlink = sem_unlink
    mod.SemLock = SemLock
    sys.modules["_multiprocessing"] = mod

    try:
        import _posixshmem  # noqa: F401
    except ImportError:
        shm = types.ModuleType("_posixshmem")

        def shm_open(*a, **k):
            raise OSError("POSIX shared memory between processes isn't available on iOS")

        shm.shm_open = shm_open
        shm.shm_unlink = shm_open
        sys.modules["_posixshmem"] = shm
