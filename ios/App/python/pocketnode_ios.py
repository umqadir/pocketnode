"""Entry points the iOS host calls (see AppDelegate.m)."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

PORT = int(os.environ.get("POCKETNODE_PORT", "7151"))

worker = None
_fault_file = None
_notes: deque[str] = deque(maxlen=4)
_started = time.time()


def _bundle() -> Path:
    return Path(sys.prefix).parent  # <App>.app/python -> <App>.app


def _caches() -> Path:
    return Path.home() / "Library" / "Caches" / "pocketnode"


def _settings() -> dict:
    try:
        import json
        return json.loads((_bundle() / "pocketnode_settings.json").read_text())
    except (OSError, ValueError):
        return {}


def _fault_log() -> str | None:
    """Arm faulthandler on a file that survives the process, and return what the
    previous run left there (a segfault in a native library, an abort)."""
    import faulthandler
    global _fault_file
    path = _caches() / "fault.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    last = None
    try:
        text = path.read_text(errors="replace").strip()
        last = (text if len(text) <= 8000 else text[:2500] + "\n...\n" + text[-5000:]) or None
    except OSError:
        pass
    _fault_file = open(path, "w")
    faulthandler.enable(file=_fault_file, all_threads=True)
    return last


def main() -> None:
    global worker
    from pocketnode import device
    from pocketnode.worker import Worker

    last_crash = _fault_log()
    token = (_bundle() / "pocketnode_token").read_text().strip()
    cores = device.core_layout()
    worker = Worker(token, slots=cores.get("logical", 6), cache_dir=str(_caches()), phone=True,
                    name=device.model(),
                    site_dir=str(Path.home() / "Library" / "Application Support" / "pocketnode" / "site"))
    worker.last_crash = last_crash
    # USB connections arrive on loopback, so that is all the app listens on
    # unless Wi-Fi was asked for when the app was built.
    wifi = bool(_settings().get("wifi"))
    listen = [f"0.0.0.0:{PORT}"] if wifi else [f"127.0.0.1:{PORT}", f"[::1]:{PORT}"]
    note(f"listening on :{PORT}" + (" (USB and Wi-Fi)" if wifi else " (USB)"))
    threading.Thread(target=_keep_computing_loop, daemon=True, name="pn-keepalive").start()
    while True:  # the app is only useful while it listens
        try:
            worker.serve(listen=listen)
        except Exception as e:  # noqa: BLE001
            note(f"listener stopped ({type(e).__name__}: {e}); restarting")
        time.sleep(1.0)


_continued = "off"  # off | asking | on | expired | refused
_refused_at = 0.0


def continued(state: str) -> str:
    """The host reports what became of a request to keep computing in the background."""
    global _continued, _refused_at
    _continued = state.split(":")[0]
    if _continued == "refused":
        _refused_at = time.time()
    note(f"background computing: {state}")
    if worker is not None:
        worker.continued = _continued == "on"
        worker.notify()
    return state


def _keep_computing_loop() -> None:
    """Hold an iOS continued-processing task while there is work, so switching
    apps doesn't suspend it; give it back after a quiet half minute. iOS only
    grants one while the app is in front, so it is asked for when work arrives."""
    import ctypes
    global _continued
    try:
        ask = ctypes.CDLL(None).pocketnode_keep_computing
    except (AttributeError, OSError):
        return
    idle_since = time.time()
    while True:
        time.sleep(1.0)
        busy = bool(worker.inflight)
        if busy:
            idle_since = time.time()
        if busy and _continued in ("off", "expired", "refused") and worker.lifecycle == "active" \
                and time.time() - _refused_at > 60:
            _continued = "asking"
            ask(1)
        elif not busy and _continued == "on" and time.time() - idle_since > 30:
            ask(0)


def lifecycle(state: str) -> str:
    if worker is not None:
        worker.notify(state)
    note(f"lifecycle: {state}")
    return state


def memory_warning() -> None:
    if worker is not None:
        worker.store.clear()
        worker.notify()
    note("memory warning: cache dropped")


def note(text: str) -> None:
    _notes.append(f"{time.strftime('%H:%M:%S')} {text}")


def tasks_done() -> int:
    return worker.tasks_done if worker else 0


def status() -> str:
    if worker is None:
        return "starting…"
    from pocketnode import device
    st = worker.state()
    mac = "connected" if worker.connected else "waiting for Mac"
    avail = st.get("avail_mem")
    lines = [
        f"Mac: {mac}",
        f"Python {sys.version.split()[0]}  slots {worker.slots}",
        f"running {st['running']}  done {worker.tasks_done}",
        f"thermal {st['thermal']}  low power {st['low_power']}",
        f"memory available {avail / 2**30:.2f} GB" if avail else "memory available ?",
        f"cache {worker.store.size / 2**20:.0f} MB",
        *_notes,
    ]
    return "\n".join(lines)
