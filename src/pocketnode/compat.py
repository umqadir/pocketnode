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
    """Let `import multiprocessing.*` succeed (torch imports it at startup).

    iOS CPython has no `_multiprocessing`: there are no child processes to
    coordinate. Importing is harmless; actually starting a process is caught by
    the guards and the task runs on the Mac instead.
    """
    try:
        import _multiprocessing  # noqa: F401
        return
    except ImportError:
        pass
    import types

    mod = types.ModuleType("_multiprocessing")
    mod.flags = {}

    def sem_unlink(name):
        raise OSError("named semaphores aren't available on iOS")

    class SemLock:
        SEM_VALUE_MAX = 2**31 - 1

        def __init__(self, *a, **k):
            raise OSError("process semaphores aren't available on iOS")

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
