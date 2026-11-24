"""What the App Store edition's iOS app calls. All state lives here; the UI only renders it.

The host app calls these functions (by name, with at most one str argument) and
gets a str back. main() runs on the app's Python thread and never returns.
Off the phone, configure() takes the directories and the secret store as
arguments, so the whole module runs (and is tested) on a Mac.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from pathlib import Path

PORT = int(os.environ.get("POCKETNODE_PORT", "7151"))
OUTPUT_CAP = 200_000
IDLE_GIVE_BACK = 30.0  # seconds without work before background computing is handed back

worker = None
_settings_file: Path | None = None
_settings: dict = {"accepting": True, "wifi": False, "keep_awake": False, "review": False}
FETCHED_LIMIT = 2000      # files listed by fetched_json
FETCHED_READ = 200_000    # bytes shown by fetched_source
_fetched_count: tuple[float, int] = (0.0, 0)  # (when counted, how many)
_build: dict = {}
_notes: deque[str] = deque(maxlen=40)
_jobs: dict[str, "_Job"] = {}
_jobs_lock = threading.Lock()
_continued = "off"  # off | asking | on | expired | refused
_compiled: dict[str, str] | None = None
_listen: list[str] = []


# ------------------------------------------------------------------ setup

def _ios_dirs() -> tuple[Path, Path]:
    lib = Path.home() / "Library"
    return lib / "Application Support" / "pocketnode", lib / "Caches" / "pocketnode"


def _bundle_settings() -> dict:
    try:
        return json.loads((Path(sys.prefix).parent / "pocketnode_settings.json").read_text())
    except (OSError, ValueError):
        return {}


def configure(app_support, caches, backend=None, build: dict | None = None, port: int | None = None,
              phone: bool = True, name: str | None = None, slots: int | None = None, last_crash: str | None = None):
    """Create the worker. app_support holds the settings (and the pair file when the
    keychain isn't available); caches holds the data cache. Returns the worker."""
    global worker, _settings_file, _settings, _build, _listen, _continued
    from . import device
    from .pairing import FileBackend, KeychainBackend, PairStore
    from .storeworker import StoreWorker
    app_support, caches = Path(app_support), Path(caches)
    app_support.mkdir(parents=True, exist_ok=True)
    caches.mkdir(parents=True, exist_ok=True)
    if backend is None:
        backend = KeychainBackend() if KeychainBackend.available() else FileBackend(app_support / "paired.json")
    _build = dict(build or {})
    _settings_file = app_support / "store-settings.json"
    # Review's first-launch default comes from the build (config key store_review).
    _settings = {"accepting": True, "wifi": False, "keep_awake": False, "review": bool(_build.get("review", False))}
    try:
        saved = json.loads(_settings_file.read_text())
        _settings.update({k: bool(saved[k]) for k in _settings if k in saved})
    except (OSError, ValueError, TypeError):
        pass
    _continued = "off"
    if slots is None:
        slots = device.core_layout().get("logical", 6) if phone else 2
    worker = StoreWorker(PairStore(backend), slots=slots, cache_dir=str(caches), phone=phone,
                         name=name or (device.model() if phone else "iPhone (store stand-in)"),
                         node_kind="phone")
    worker.last_crash = last_crash
    worker.accepting = _settings["accepting"]
    worker._wifi_wanted = _settings["wifi"]
    worker.review = _settings["review"]
    worker.set_approvals_file(app_support / "approvals.json")
    p = PORT if port is None else port
    _listen = [f"127.0.0.1:{p}", f"[::1]:{p}"] if p else ["127.0.0.1:0"]
    _debug_pairing()
    return worker


def _debug_build() -> bool:
    try:
        f = ctypes.CDLL(None).pocketnode_debug_build
    except (OSError, AttributeError):
        return False
    f.restype = ctypes.c_int
    return f() == 1


def _debug_pairing() -> None:
    """Debug builds only: POCKETNODE_DEBUG_PAIR_CODE opens a window with that code that
    stays open and answers Allow by itself (for automated runs on the simulator)."""
    code = os.environ.get("POCKETNODE_DEBUG_PAIR_CODE", "")
    if not (_build.get("debug") and _debug_build() and len(code) == 6 and code.isdigit()):
        return
    worker.window.auto_allow = True
    worker.window.begin(code=code, keep_open=True)
    note("debug build: pairing window open with the code from POCKETNODE_DEBUG_PAIR_CODE")


def serve(listen: list[str] | None = None) -> None:
    """Serve until the process ends (restarting the listener if it fails)."""
    where = listen or _listen
    note("listening on " + ", ".join(where) + " (USB)")
    threading.Thread(target=_give_back_loop, daemon=True, name="pn-giveback").start()
    while True:
        try:
            worker.serve(listen=where)
        except Exception as e:  # noqa: BLE001
            note(f"listener stopped ({type(e).__name__}: {e}); restarting")
        time.sleep(1.0)


def main() -> str:
    from . import device
    last = None
    try:  # what the previous run's fault handler left, as in the sideloaded app
        import pocketnode_ios
        last = pocketnode_ios._fault_log()
    except Exception:  # noqa: BLE001
        pass
    support, caches = _ios_dirs()
    configure(support, caches, build=_bundle_settings(), phone=True, name=device.model(), last_crash=last)
    serve()
    return ""


def _save_settings() -> None:
    if _settings_file is None:
        return
    try:
        tmp = _settings_file.with_name(_settings_file.name + ".tmp")
        tmp.write_text(json.dumps(_settings))
        os.replace(tmp, _settings_file)
    except OSError as e:
        note(f"couldn't save settings ({e})")


def _flag(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------ status

def note(text: str) -> str:
    _notes.append(f"{time.strftime('%H:%M:%S')} {text}")
    return ""


_BINARY = (".so", ".dylib", ".fwork")


def _is_compiled(d) -> bool:
    """Does a distribution carry compiled code? In the iOS app each extension module has been
    moved into Frameworks and a .fwork marker left in its place, and RECORD may still name the
    original .so, so what is on disk under its top-level packages decides; RECORD is the fallback."""
    from pathlib import PurePosixPath
    try:
        base = Path(d.locate_file(""))
    except Exception:  # noqa: BLE001
        base = None
    files = list(d.files or ())
    tops = (d.read_text("top_level.txt") or "").split()
    if not tops:
        tops = sorted({PurePosixPath(str(f)).parts[0].split(".")[0] for f in files
                       if PurePosixPath(str(f)).parts
                       and not PurePosixPath(str(f)).parts[0].endswith((".dist-info", ".data", ".egg-info"))
                       and PurePosixPath(str(f)).parts[0] not in ("..", "__pycache__")})
    if base is not None and base.is_dir():
        for t in tops:
            p = base / t
            try:
                if p.is_dir() and any(f.name.endswith(_BINARY) for f in p.rglob("*")):
                    return True
                if any(f.name.endswith(_BINARY) for f in base.glob(f"{t}.*")):  # a top-level module
                    return True
            except OSError:
                continue
    return any(str(f).endswith(_BINARY) for f in files)


def _compiled_packages(dists=None) -> dict[str, str]:
    """Distributions in the app that carry compiled code (fixed at build time, so read once)."""
    global _compiled
    if _compiled is None or dists is not None:
        import importlib.metadata as md
        out = {}
        for d in (md.distributions() if dists is None else dists):
            n = d.metadata["Name"]
            if n and n not in out and _is_compiled(d):
                out[n] = d.version
        found = dict(sorted(out.items(), key=lambda kv: kv[0].lower()))
        if dists is not None:
            return found
        _compiled = found
    return _compiled


def _version() -> str:
    try:
        import importlib.metadata as md
        return md.version("pocketnode")
    except Exception:  # noqa: BLE001 - the app carries the source, not an installed distribution
        from . import envinfo
        return f"source {envinfo.source_version()}"


def status() -> dict:
    w = worker
    if w is None:
        return {"ready": False}
    st = w.state()
    return {
        "ready": bool(w.ready), "accepting": bool(w.accepting), "port": int(w.port or PORT),
        "wifi": w.wifi, "wifi_address": w.wifi_address(), "keep_awake": bool(_settings["keep_awake"]),
        "sessions": w.sessions_info(), "paired": w.pairs.list(), "tasks": w.tasks_info(),
        "done": int(w.tasks_done), "thermal": str(st.get("thermal", "nominal")),
        "low_power": bool(st.get("low_power")), "avail_mem": st.get("avail_mem"),
        "cache_bytes": int(w.store.size), "background": _continued, "pairing": w.window.state(),
        "packages": _compiled_packages(), "python": platform.python_version(), "version": _version(),
        "tls": bool(_build.get("tls", True)), "privacy_url": _build.get("privacy_url"),
        "notes": list(_notes)[-8:],
        "review": bool(w.review), "waiting": w.waiting_info(), "code_count": len(w.codes),
        "fetched_count": _count_fetched(),
    }


def status_json(_arg=None) -> str:
    return json.dumps(status())


# ------------------------------------------------------------------ actions

def pair_begin(_arg=None) -> str:
    code = worker.window.begin()
    note("pairing window open")
    return code


def pair_cancel(_arg=None) -> str:
    worker.window.cancel()
    return ""


def pair_decide(allow) -> str:
    worker.window.decide(_flag(allow))
    note("pairing allowed" if _flag(allow) else "pairing declined")
    return ""


def forget(pair_id) -> str:
    name = worker.pairs.name(pair_id)
    ok = worker.forget(str(pair_id))
    if ok:
        note(f"forgot {name or pair_id}")
    return "1" if ok else "0"


def set_accepting(on) -> str:
    _settings["accepting"] = _flag(on)
    _save_settings()
    worker.set_accepting(_settings["accepting"])
    note("accepting work" if _settings["accepting"] else "paused: not accepting work")
    return ""


def stop_tasks(_arg=None) -> str:
    n = worker.stop_tasks()
    note(f"stopped {n} task{'s' if n != 1 else ''}")
    return str(n)


def set_wifi(on) -> str:
    _settings["wifi"] = _flag(on)
    _save_settings()
    try:
        worker.set_wifi(_settings["wifi"])
    except OSError as e:
        note(f"Wi-Fi listener failed ({e})")
        return str(e)
    note("Wi-Fi on" if _settings["wifi"] else "Wi-Fi off")
    return ""


def set_keep_awake(on) -> str:
    _settings["keep_awake"] = _flag(on)
    _save_settings()
    return ""


def task_source(task_id) -> str:
    return worker.task_source(str(task_id)) if worker else ""


# ------------------------------------------------------- code made visible

def set_review(on) -> str:
    """"Ask before running new code": a Mac's own new code waits for Allow."""
    _settings["review"] = _flag(on)
    _save_settings()
    worker.set_review(_settings["review"])
    note("asking before running new code" if _settings["review"] else "running code without asking")
    return ""


def approve(arg) -> str:
    """"<code> 1" allows that code, "<code> 0" declines it (for every Mac that sent it)."""
    parts = str(arg).split()
    if len(parts) != 2:
        return "expected '<code> 1' or '<code> 0'"
    code, allow = parts[0], _flag(parts[1])
    label = (worker.codes.get(code) or {}).get("label", code)
    worker.approve(code, allow)
    note(f"{'allowed' if allow else 'declined'} {label}")
    return ""


def code_list(_arg=None) -> str:
    return json.dumps(worker.code_list() if worker else [])


def code_source(code) -> str:
    return worker.code_source(str(code)) if worker else ""


def _fetched_root() -> Path | None:
    """Where the worker's import hook keeps the pure-Python modules it fetched from the Mac."""
    if worker is None:
        return None
    return Path(worker.finder.cache_dir).resolve()


def _fetched_files():
    root = _fetched_root()
    if root is None or not root.is_dir():
        return root, []
    files = []
    for p in root.rglob("*.py"):
        if p.is_file() and not p.is_symlink():
            files.append(p)
    return root, sorted(files, key=lambda p: p.relative_to(root).as_posix())


def _count_fetched() -> int:
    global _fetched_count
    when, n = _fetched_count
    if time.monotonic() - when > 5.0:  # status is polled often; the walk needn't be
        n = len(_fetched_files()[1])
        _fetched_count = (time.monotonic(), n)
    return n


def fetched_json(_arg=None) -> str:
    """[{"path", "module", "bytes"}] for the files fetched from the Mac, sorted by path."""
    root, files = _fetched_files()
    out = []
    for p in files[:FETCHED_LIMIT]:
        rel = p.relative_to(root)
        # Each fetched package lives in "<name>-<hash>/"; inside it, the path is the module.
        parts = list(rel.parts[1:]) if len(rel.parts) > 1 else list(rel.parts)
        parts[-1] = parts[-1][:-3]
        if parts[-1] == "__init__" and len(parts) > 1:
            parts = parts[:-1]
        try:
            size = p.stat().st_size
        except OSError:
            continue
        out.append({"path": rel.as_posix(), "module": ".".join(parts), "bytes": size})
    return json.dumps(out)


def fetched_source(path) -> str:
    """A fetched file's text (first 200 KB). Paths outside the cache are refused ("")."""
    root = _fetched_root()
    if root is None:
        return ""
    try:
        p = (root / str(path)).resolve()
        if not p.is_relative_to(root) or p == root or not p.is_file():
            return ""
        with open(p, "rb") as f:
            data = f.read(FETCHED_READ)
    except (OSError, ValueError):
        return ""
    return data.decode("utf-8", "replace")


DEMOS = [
    {"title": "Primes (pure Python)", "code": '''import time
t = time.perf_counter()
n = 200_000
sieve = bytearray([1]) * (n + 1)
sieve[0:2] = b"\\x00\\x00"
for i in range(2, int(n ** 0.5) + 1):
    if sieve[i]:
        sieve[i * i::i] = bytes(len(range(i * i, n + 1, i)))
print(f"{sum(sieve)} primes below {n:,} in {time.perf_counter() - t:.3f} s")
'''},
    {"title": "Matrix multiply (numpy)", "code": '''try:
    import numpy as np
except ImportError:
    print("numpy isn't in this build of the app.")
else:
    import time
    a = np.random.default_rng(0).standard_normal((1000, 1000))
    t = time.perf_counter()
    b = a @ a
    s = time.perf_counter() - t
    print(f"1000 x 1000 matrix product in {s:.3f} s ({2e9 / s / 1e9:.1f} GFLOPS)")
'''},
    {"title": "What's in this app", "code": '''import importlib.metadata as md
import sys
print("Python", sys.version.split()[0])
names = {d.metadata["Name"]: d.version for d in md.distributions() if d.metadata["Name"]}
for name in sorted(names, key=str.lower):
    print(name, names[name])
'''},
    {"title": "Monte Carlo pi", "code": '''import random
import time
t = time.perf_counter()
n = 1_000_000
inside = sum(1 for _ in range(n) if random.random() ** 2 + random.random() ** 2 <= 1)
print(f"pi is about {4 * inside / n:.4f} ({n:,} samples, {time.perf_counter() - t:.2f} s)")
'''},
]


def demo_snippets(_arg=None) -> str:
    return json.dumps(DEMOS)


# ---------------------------------------------------------------- local runs

class _CappedIO:
    """Collects output up to OUTPUT_CAP characters, then notes that the rest was dropped."""

    def __init__(self):
        self.parts: list[str] = []
        self.n = 0
        self.dropped = False

    def write(self, s: str) -> int:
        if self.n < OUTPUT_CAP:
            s2 = s[:OUTPUT_CAP - self.n]
            self.parts.append(s2)
            self.n += len(s2)
            self.dropped |= len(s2) < len(s)
        else:
            self.dropped = True
        return len(s)

    def getvalue(self) -> str:
        text = "".join(self.parts)
        return text + ("\n[output cut at 200 KB]\n" if self.dropped else "")


class _Job:
    def __init__(self, code: str):
        self.id = uuid.uuid4().hex[:12]
        self.code = code
        self.out = _CappedIO()
        self.error: str | None = None
        self.done = False
        self.start = time.perf_counter()
        self.end: float | None = None
        self.ident: int | None = None
        self.lock = threading.Lock()
        self.running = False  # inside user code: a cancel may be posted only then


def _run_job(job: _Job) -> None:
    from . import worker as wmod
    from .errors import TaskCancelled
    wmod._install_streams()
    # Something else may have replaced sys.stdout since (a test runner does): put the
    # thread-diverting streams back in front of whatever is there now.
    for name, s in (("stdout", wmod._out), ("stderr", wmod._err)):
        if getattr(sys, name) is not s:
            s.real = getattr(sys, name)
            setattr(sys, name, s)
    wmod._out.local.buf = wmod._err.local.buf = job.out
    try:
        try:
            code = compile(job.code, "<phone>", "exec")
            with job.lock:
                job.ident = threading.get_ident()
                job.running = True
            try:
                exec(code, {"__name__": "__main__", "__builtins__": __builtins__})
            finally:
                _leave(job)
        except TaskCancelled:
            job.error = "stopped"
        except SystemExit as e:
            if e.code not in (None, 0):
                job.error = f"SystemExit: {e.code}"
        except BaseException as e:  # noqa: BLE001 - shown to the person who typed it
            tb = traceback.extract_tb(e.__traceback__)
            tb = [f for f in tb if f.filename == "<phone>"]
            lines = traceback.format_list(tb) + traceback.format_exception_only(type(e), e)
            job.error = "".join(lines).rstrip()
    except TaskCancelled:  # landed after the code finished
        job.error = job.error or "stopped"
    finally:
        wmod._out.local.buf = wmod._err.local.buf = None
        job.end = time.perf_counter()
        job.done = True


def _leave(job: _Job) -> None:
    """Leave the cancellable region; a cancel not yet delivered is withdrawn."""
    from .errors import TaskCancelled
    while True:
        try:
            with job.lock:
                job.running = False
                ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(threading.get_ident()), None)
            return
        except TaskCancelled:
            continue


def run_local(code) -> str:
    """Run code typed on the phone, on its own thread. Returns the job id."""
    job = _Job(str(code))
    with _jobs_lock:
        _jobs[job.id] = job
        while len(_jobs) > 20:
            old = next((k for k, j in _jobs.items() if j.done), None)
            if old is None:
                break
            del _jobs[old]
    threading.Thread(target=_run_job, args=(job,), daemon=True, name="pn-local").start()
    return job.id


def local_poll(job_id) -> str:
    job = _jobs.get(str(job_id))
    if job is None:
        return json.dumps({"done": True, "out": "", "error": "no such run", "seconds": 0.0})
    secs = (job.end or time.perf_counter()) - job.start
    return json.dumps({"done": job.done, "out": job.out.getvalue(), "error": job.error,
                       "seconds": round(secs, 3)})


def local_cancel(job_id) -> str:
    from .errors import TaskCancelled
    job = _jobs.get(str(job_id))
    if job is None:
        return ""
    with job.lock:
        if job.running and job.ident:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(job.ident), ctypes.py_object(TaskCancelled))
    return ""


# ------------------------------------------------------- the app's lifecycle

def lifecycle(state) -> str:
    if worker is not None:
        worker.notify(str(state))
    note(f"lifecycle: {state}")
    return str(state)


def memory_warning(_arg=None) -> str:
    if worker is not None:
        worker.store.clear()
        worker.notify()
    note("memory warning: cache dropped")
    return ""


def continued(state) -> str:
    """The host reports what became of its request to keep computing in the background."""
    global _continued
    _continued = str(state).split(":")[0]
    note(f"background computing: {state}")
    if worker is not None:
        worker.continued = _continued == "on"
        worker.notify()
    return str(state)


def tasks_done(_arg=None) -> str:
    return str(worker.tasks_done if worker else 0)


def _busy() -> bool:
    if worker is not None and worker.inflight:
        return True
    return any(not j.done for j in list(_jobs.values()))


def _give_back_loop() -> None:
    """The person asks for background computing in the UI; it is given back after 30 idle seconds."""
    try:
        ask = ctypes.CDLL(None).pocketnode_keep_computing
    except (OSError, AttributeError):
        return
    idle_since = time.time()
    while True:
        time.sleep(1.0)
        if _busy():
            idle_since = time.time()
        elif _continued == "on" and time.time() - idle_since > IDLE_GIVE_BACK:
            ask(0)
            idle_since = time.time()
