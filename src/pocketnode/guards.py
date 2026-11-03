"""Platform guards for phone workers: never silently wrong.

A task that tries to do something the phone genuinely can't do the way the Mac
would (start a process, read a file that lives on the Mac, write a file the user
expects on the Mac, import a compiled module the phone lacks) must not quietly
take a different path. The guard records the event against the running task
and raises an exception that `except Exception:` won't swallow. Even if a bare
`except:` catches it, the recorded trip still sends the task back to the Mac.
"""

from __future__ import annotations

import builtins
import io
import os
import sys
import threading

from .errors import Unavailable

_local = threading.local()


class _Escape(BaseException):
    """Carries an Unavailable past `except Exception` blocks in user code."""

    def __init__(self, err: Unavailable):
        super().__init__(str(err))
        self.err = err


def begin_task() -> None:
    _local.trip = None


def end_task() -> Unavailable | None:
    t = getattr(_local, "trip", None)
    if hasattr(_local, "trip"):
        del _local.trip
    return t


def trip(kind: str, detail: str, module: str | None = None, raise_: bool = True) -> None:
    err = Unavailable(kind, detail, module)
    if getattr(_local, "trip", None) is None:
        _local.trip = err
    if raise_:
        raise _Escape(err)


def in_task() -> bool:
    return hasattr(_local, "trip")


class Roots:
    """Paths that exist on the phone. Anything else is assumed to be on the Mac."""

    def __init__(self, roots: list[str]):
        rs = {os.path.normpath(r) for r in roots if r} | {os.path.realpath(r) for r in roots if r}
        self.roots = tuple(r.rstrip("/") + "/" for r in rs)

    def local(self, path: str) -> bool:
        for p in (os.path.normpath(os.path.abspath(path)), os.path.realpath(path)):
            if any(p.startswith(r) or p + "/" == r for r in self.roots):
                return True
        return False


# Where Mac data lives. These don't exist on the phone, so a task touching them
# means the user meant the Mac's file.
_MAC_PREFIXES = ("/Users/", "/Volumes/", "/opt/", "/Applications/", "/Library/", "/tmp/", "/private/tmp/",
                 "/var/folders/", "/private/var/folders/", "/home/", "/usr/local/", "/Network/")

_roots: Roots | None = None
_scratch: str = ""
_phone_dirs: tuple[str, ...] = ()
# Path helpers that just forward a caller's request; look past them for the caller.
_FORWARDERS = ("os.py", "genericpath.py", "posixpath.py", "pathlib", "shutil.py", "glob.py", "fnmatch.py",
               "guards.py")


def _caller_is_user() -> bool:
    """Is the nearest real caller the user's own code (not stdlib or a library)?"""
    f = sys._getframe(3)
    while f is not None:
        fn = f.f_code.co_filename
        if any(fn.endswith(x) or f"/{x}/" in fn for x in _FORWARDERS):
            f = f.f_back
            continue
        if fn.startswith("<frozen"):
            return False
        return not any(fn.startswith(d) for d in _phone_dirs)
    return False


def _check(file, writing: bool, op: str):
    if _roots is None or isinstance(file, int) or not in_task() or getattr(_local, "checking", False):
        return
    _local.checking = True  # realpath() below calls the patched os.lstat
    try:
        _check_path(file, writing, op)
    finally:
        _local.checking = False


def _check_path(file, writing: bool, op: str):
    # Metadata lookups (stat, listdir, access) on Mac paths are routine inside
    # libraries (linecache checks source files); they just fail. Only the
    # user's own code asking about a Mac path means the answer would differ.
    if op.startswith("os.") and not writing and op != "os.open" and not _caller_is_user():
        return
    try:
        path = os.fsdecode(os.fspath(file))
    except TypeError:
        return
    if path in ("", "."):
        return
    if not os.path.isabs(path):
        trip("mac-file", f"{op}({path!r}) uses a relative path, which means the Mac's working directory")
    if _roots.local(path):
        return
    if writing or path.startswith(_MAC_PREFIXES):
        what = "writes" if writing else "reads"
        trip("mac-file", f"{op} {what} {path}, which is on the Mac, not the phone")
    # Anything else (/proc, /sys, ...) gets the OS's own answer, as on the Mac.


def install(roots: list[str], scratch: str, phone_dirs: tuple[str, ...] = ()) -> None:
    """Patch process creation and filesystem entry points. Idempotent."""
    global _roots, _scratch, _phone_dirs
    _phone_dirs = tuple(os.path.realpath(d) for d in phone_dirs) + tuple(phone_dirs)
    if getattr(builtins, "_pocketnode_guarded", False):
        _roots = Roots(roots)
        return
    _roots = Roots(roots)
    _scratch = scratch
    builtins._pocketnode_guarded = True

    real_open = builtins.open

    def open(file, mode="r", *a, **k):
        _check(file, any(c in mode for c in "wax+"), "open")
        return real_open(file, mode, *a, **k)

    builtins.open = open
    io.open = open

    real_os_open = os.open

    def os_open(path, flags, *a, **k):
        _check(path, bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)), "os.open")
        return real_os_open(path, flags, *a, **k)

    os.open = os_open

    for name in ("stat", "lstat", "listdir", "scandir", "access", "remove", "unlink", "mkdir",
                 "makedirs", "rename", "replace", "rmdir"):
        real = getattr(os, name, None)
        if real is None:
            continue
        writing = name not in ("stat", "lstat", "listdir", "scandir", "access")

        def wrap(real=real, name=name, writing=writing):
            def f(path=".", *a, **k):
                _check(path, writing, f"os.{name}")
                return real(path, *a, **k)
            f.__name__ = name
            return f

        setattr(os, name, wrap())

    def no_process(what):
        def f(*a, **k):
            trip("process", f"{what} needs a child process; iOS apps can't create them")
        return f

    for name in ("fork", "forkpty", "posix_spawn", "posix_spawnp", "system", "popen",
                 "execv", "execve", "execvp", "execvpe", "spawnv", "spawnve"):
        if hasattr(os, name):
            setattr(os, name, no_process(f"os.{name}"))
    try:
        import _posixsubprocess
        _posixsubprocess.fork_exec = no_process("subprocess")
    except ImportError:
        pass
    import subprocess
    subprocess.Popen._execute_child = lambda self, *a, **k: no_process("subprocess.Popen")()
    # joblib inside a task falls back to threads rather than trying processes.
    pass


def user_frame_import(top: str, phone_dirs: tuple[str, ...]) -> bool:
    """Was `import top` executed by the user's own code (not a library)?"""
    f = sys._getframe(2)
    while f is not None:
        fn = f.f_code.co_filename
        if fn.startswith("<frozen importlib") or "importlib" in fn and fn.endswith(("__init__.py", "_bootstrap.py")):
            f = f.f_back
            continue
        return not any(fn.startswith(d) for d in phone_dirs)
    return False
