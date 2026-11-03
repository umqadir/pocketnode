"""Where nodes come from: local worker processes, a USB-connected phone, an address."""

from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import usbmux
from .executor import PocketExecutor, Source, attach

log = logging.getLogger("pocketnode")

CONFIG_DIR = Path(os.environ.get("POCKETNODE_HOME", Path.home() / ".pocketnode"))
BUNDLE_ID = "dev.pocketnode.app"
PHONE_PORT = 7151


def pairing_token() -> str:
    """The token shared with the phone app (baked into it when it's built)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    f = CONFIG_DIR / "token"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip()


class LocalWorkers(Source):
    """N worker processes on this Mac, one slot each, sharing its environment."""

    same_env = True
    bandwidth_guess = 4e9
    latency_guess = 0.0002

    def __init__(self, n: int | None = None, phone_like: bool = False, kind: str = "mac",
                 name: str | None = None, slots: int = 1, cache_dir: str | None = None,
                 python: str | None = None):
        self.python = python or sys.executable
        self.n = n if n is not None else (os.cpu_count() or 2)
        self.phone_like = phone_like
        self.kind = kind
        self.name = name
        self.slots = slots
        self.cache_dir = cache_dir
        self.procs: list[subprocess.Popen] = []
        self.token = secrets.token_hex(16)
        self._dir = tempfile.mkdtemp(prefix="pn-")
        self._stop = False
        if phone_like:
            self.same_env = False
            self.bandwidth_guess = PhoneLink.bandwidth_guess
            self.latency_guess = PhoneLink.latency_guess

    def describe(self) -> str:
        return "local workers"

    def start(self, ex: PocketExecutor) -> None:
        path = os.path.join(self._dir, "sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(64)
        self._srv = srv
        tok = os.path.join(self._dir, "token")
        Path(tok).write_text(self.token)
        threads = max(1, (os.cpu_count() or 1) // max(1, self.n)) if not self.phone_like else None
        env = dict(os.environ)
        if threads:
            for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                      "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
                env.setdefault(v, str(threads))
        env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).resolve().parent.parent),
                                             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        cmd = [self.python, "-m", "pocketnode.worker", "--connect", f"unix:{path}",
               "--token-file", tok, "--slots", str(self.slots), "--kind", self.kind]
        if self.phone_like:
            cmd.append("--phone")
        if self.name:
            cmd += ["--name", self.name]
        if self.cache_dir:
            cmd += ["--cache-dir", self.cache_dir]
        self._cmd, self._env = cmd, env
        for _ in range(self.n):
            self._spawn()

        def accept():
            while not self._stop:
                try:
                    s, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=self._attach, args=(ex, s), daemon=True).start()

        threading.Thread(target=accept, daemon=True, name="pn-local-accept").start()
        threading.Thread(target=self._watch, args=(ex,), daemon=True, name="pn-local-watch").start()

    def _spawn(self):
        p = subprocess.Popen(self._cmd, env=self._env, stdin=subprocess.DEVNULL)
        self.procs.append(p)

    def _attach(self, ex, s):
        try:
            attach(ex, s, self, self.token)
        except Exception as e:
            log.debug("local worker failed handshake: %s", e)
            s.close()

    def _watch(self, ex):
        # Respawn workers that die (a segfault in a C extension, an OOM kill).
        while not self._stop:
            time.sleep(1.0)
            for p in list(self.procs):
                if p.poll() is not None and not self._stop:
                    self.procs.remove(p)
                    self._spawn()

    def stop(self) -> None:
        self._stop = True
        for p in self.procs:
            p.terminate()
        try:
            self._srv.close()
        except Exception:
            pass


class SimulatedPhone(LocalWorkers):
    """A local stand-in for the phone: phone guards, phone placement, its own env.

    Point `python` at an interpreter whose environment holds only what the phone
    has, and routing, import fetching and fallbacks behave as with the device.
    """

    def __init__(self, python: str | None = None, slots: int = 6, cache_dir: str | None = None):
        super().__init__(n=1, phone_like=True, kind="phone", name="iPhone (simulated)", slots=slots,
                         cache_dir=cache_dir or tempfile.mkdtemp(prefix="pn-simphone-"), python=python)

    def describe(self) -> str:
        return "simulated phone"


class Address(Source):
    """A worker at host:port (the phone over Wi-Fi, or anything else). Reconnects."""

    def __init__(self, addr: str, token: str | None = None, retry: float = 2.0):
        self.addr = addr
        self.token = token or pairing_token()
        self.retry = retry
        self._stop = False

    def describe(self) -> str:
        return self.addr

    def start(self, ex):
        threading.Thread(target=self._loop, args=(ex,), daemon=True, name=f"pn-addr-{self.addr}").start()

    def _connect(self):
        host, port = self.addr.rsplit(":", 1)
        return socket.create_connection((host.strip("[]"), int(port)), timeout=5)

    def _loop(self, ex):
        while not self._stop:
            try:
                s = self._connect()
                s.settimeout(None)
                node = attach(ex, s, self, self.token)
                while node.connected and not self._stop:
                    time.sleep(0.5)
            except Exception as e:
                log.debug("pocketnode: %s not reachable: %s", self.addr, e)
            time.sleep(self.retry)

    def stop(self):
        self._stop = True


class PhoneLink(Source):
    """The phone over USB via usbmuxd. Follows plug/unplug; relaunches the app if needed."""

    bandwidth_guess = 35e6  # refined by measurement
    latency_guess = 0.003

    def __init__(self, udid: str | None = None, port: int = PHONE_PORT, token: str | None = None,
                 relaunch: bool = True):
        self.udid = udid
        self.port = port
        self.token = token or pairing_token()
        self.relaunch = relaunch
        self._stop = False
        self._last_launch = 0.0
        self.status = "waiting for a phone"

    def describe(self) -> str:
        return f"usb:{self.udid or 'any'}"

    def start(self, ex):
        threading.Thread(target=self._loop, args=(ex,), daemon=True, name="pn-usb").start()

    def _loop(self, ex):
        failures = 0
        while not self._stop:
            try:
                devs = [d for d in usbmux.list_devices() if self.udid in (None, d.serial)]
            except OSError:
                devs = []
            usb = [d for d in devs if d.connection == "USB"] or devs
            if not usb:
                self.status = "no phone attached"
                time.sleep(1.0)
                continue
            dev = usb[0]
            try:
                s = usbmux.connect(dev.id, self.port)
                node = attach(ex, s, self, self.token)
                failures = 0
                self.status = f"connected to {node.name}"
                while node.connected and not self._stop:
                    time.sleep(0.3)
            except PermissionError as e:
                self.status = f"phone rejected pairing: {e}"
                log.warning("pocketnode: %s", self.status)
                time.sleep(10)
            except Exception:
                failures += 1
                self.status = "phone attached, app not answering"
                if self.relaunch and failures >= 2 and time.monotonic() - self._last_launch > 30:
                    self._last_launch = time.monotonic()
                    launch_app(dev.serial)
                time.sleep(1.0)

    def stop(self):
        self._stop = True


def launch_app(udid: str) -> bool:
    """Start (or foreground) the pocketnode app on the phone over USB."""
    try:
        r = subprocess.run(["xcrun", "devicectl", "device", "process", "launch", "--device", udid,
                            BUNDLE_ID], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            log.info("pocketnode: launched the app on the phone")
            return True
        log.debug("devicectl launch failed: %s", r.stderr.strip())
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("devicectl unavailable: %s", e)
    return False
