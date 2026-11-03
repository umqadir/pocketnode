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
_notes: deque[str] = deque(maxlen=4)
_started = time.time()


def _bundle() -> Path:
    return Path(sys.prefix).parent  # <App>.app/python -> <App>.app


def _caches() -> Path:
    return Path.home() / "Library" / "Caches" / "pocketnode"


def main() -> None:
    global worker
    from pocketnode import device
    from pocketnode.worker import Worker

    token = (_bundle() / "pocketnode_token").read_text().strip()
    cores = device.core_layout()
    worker = Worker(token, slots=cores.get("logical", 6), cache_dir=str(_caches()), phone=True,
                    name=device.model())
    note(f"listening on :{PORT}")
    try:
        import importlib
        importlib.import_module("pocketnode_ios_experiments")  # registers diagnostics if bundled
    except ImportError:
        pass
    worker.serve(listen=f"0.0.0.0:{PORT}")


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
    mac = "connected" if worker.conn is not None else "waiting for Mac"
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
