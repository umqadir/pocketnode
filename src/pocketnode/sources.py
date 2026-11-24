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
from .pairing import NotPaired, PairingRevoked
from .wire import Refused

log = logging.getLogger("pocketnode")

CONFIG_DIR = Path(os.environ.get("POCKETNODE_HOME", Path.home() / ".pocketnode"))
SIM_BUNDLE_ID = "dev.pocketnode.app"
PHONE_PORT = 7151

UNPAIRED = ("this iPhone runs the App Store edition of pocketnode and isn't paired with this Mac: "
            "in the app tap Pair a Mac, then run `pocketnode pair`")
REVOKED = "the iPhone no longer has this Mac's pairing: run `pocketnode pair` again"
OPEN_APP = "open the pocketnode app on the iPhone"


def app_config() -> dict:
    """The app's build settings (~/.pocketnode/app.json)."""
    import json
    try:
        return json.loads((CONFIG_DIR / "app.json").read_text())
    except (OSError, ValueError):
        return {}


def bundle_id(sdk: str = "iphoneos", team: str | None = None, cfg: dict | None = None) -> str:
    """The app's bundle identifier.

    An explicit `bundle_id` in the config (`--bundle-id`) wins. Otherwise a
    device build uses one derived from the signing team, because a bundle
    identifier belongs to the first team that registers it: dev.pocketnode.<team>.
    The value is saved per team at the first device build, so it stays the same.
    The simulator needs no registration and uses dev.pocketnode.app.
    """
    cfg = app_config() if cfg is None else cfg
    if cfg.get("bundle_id"):
        return cfg["bundle_id"]
    if sdk == "iphonesimulator":
        return SIM_BUNDLE_ID
    team = team or cfg.get("team")
    if not team:
        return SIM_BUNDLE_ID
    return cfg.get("bundle_ids", {}).get(team) or f"dev.pocketnode.{team.lower()}"


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
        cmd += self.extra_args()
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

    def extra_args(self) -> list[str]:
        return []

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

    def __init__(self, python: str | None = None, slots: int = 6, cache_dir: str | None = None,
                 edition: str | None = None):
        super().__init__(n=1, phone_like=True, kind="phone", name="iPhone (simulated)", slots=slots,
                         cache_dir=cache_dir or tempfile.mkdtemp(prefix="pn-simphone-"), python=python)
        # "store" stands in for the App Store edition: paired at run time, fixed packages.
        self.edition = edition or os.environ.get("POCKETNODE_STANDIN_EDITION") or "dev"
        if self.edition == "store":
            from .pairing import seed_pair_file
            self._pair_file = os.path.join(self._dir, "pairs.json")
            self._cred = seed_pair_file(self._pair_file, "this Mac")

    def credentials(self, hello: dict):
        c = getattr(self, "_cred", None)
        if c is None or hello.get("phone_id") != c["phone_id"]:
            return None
        return c["token"], {"pair_id": c["pair_id"]}

    def extra_args(self) -> list[str]:
        if self.edition == "store":
            return ["--edition", "store", "--pair-file", self._pair_file]
        return []

    def describe(self) -> str:
        return "simulated phone"


class Address(Source):
    """A worker at host:port (the phone over Wi-Fi, or anything else). Reconnects."""

    def __init__(self, addr: str, token: str | None = None, retry: float = 2.0, credentials=None):
        self.addr = addr
        self.token = token  # None: the dev token, made only if a dev phone answers
        self.retry = retry
        self.credentials = credentials  # App Store edition: (token, extra) for a hello; default the paired.json
        self._stop = False
        self.status = "connecting"

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
                node = attach(ex, s, self, self.token or pairing_token)
                self.status = f"connected to {node.name}"
                while node.connected and not self._stop:
                    time.sleep(0.5)
            except Refused as e:
                self.status = str(e)
                ex._note(("refused", self.addr), f"pocketnode: {self.addr}: {e}")
                time.sleep(max(self.retry, 5.0))
            except NotPaired:
                self.status = UNPAIRED
                ex._note(("unpaired", self.addr), f"pocketnode: {UNPAIRED}")
                time.sleep(max(self.retry, 3.0))
            except PairingRevoked:
                self.status = REVOKED
                ex._note(("revoked", self.addr), f"pocketnode: {REVOKED}")
                time.sleep(max(self.retry, 5.0))
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
        self.token = token  # None: the dev token, made only if a dev phone answers
        self.relaunch = relaunch
        self.store_seen = False  # a phone here runs the App Store edition
        self._stop = False
        self._last_launch = 0.0
        self._launches = 0
        self.status = "waiting for a phone"

    def describe(self) -> str:
        return f"usb:{self.udid or 'any'}"

    def start(self, ex):
        self._warn_expiry(ex)
        threading.Thread(target=self._loop, args=(ex,), daemon=True, name="pn-usb").start()

    def _warn_expiry(self, ex) -> None:
        """Free Apple accounts sign for 7 days. Say so before the app stops launching."""
        import datetime
        import json
        try:
            exp = json.loads((CONFIG_DIR / "app.json").read_text()).get("profile_expires")
            left = datetime.datetime.fromisoformat(exp) - datetime.datetime.now()
        except (OSError, ValueError, TypeError):
            return
        if left < datetime.timedelta(0):
            ex._note(("expiry",), "pocketnode: the phone app's signing expired on "
                     f"{exp[:10]}; reinstall it with `pocketnode app install`")
        elif left < datetime.timedelta(days=2):
            ex._note(("expiry",), f"pocketnode: the phone app's signing expires {exp[:16].replace('T', ' ')}; "
                     "`pocketnode app install` renews it (a good moment for any package changes too)")

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
            # Several devices may be attached (an iPad, a second phone): the one
            # running the app is the one that answers on its port.
            node = None
            pairing = False  # the app answered but this Mac isn't (or no longer) paired: no relaunching
            for dev in usb:
                try:
                    s = usbmux.connect(dev.id, self.port)
                    node = attach(ex, s, self, self.token or pairing_token)
                    self.store_seen |= node.hello.get("edition") == "store"
                    break
                except Refused as e:
                    self.store_seen |= (getattr(e, "hello", None) or {}).get("edition") == "store"
                    self.status = str(e)
                    ex._note(("refused", dev.serial), f"pocketnode: {e}")
                    time.sleep(5)
                except NotPaired:
                    pairing = self.store_seen = True
                    self.status = UNPAIRED
                    ex._note(("unpaired", dev.serial), f"pocketnode: {UNPAIRED}")
                except PairingRevoked:
                    pairing = self.store_seen = True
                    self.status = REVOKED
                    ex._note(("revoked", dev.serial), f"pocketnode: {REVOKED}")
                except PermissionError:
                    self.status = ("the phone app was built with a different pairing token; "
                                   "reinstall it with `pocketnode app install`")
                    ex._note(("pairing", dev.serial), f"pocketnode: {self.status}")
                    time.sleep(10)
                except Exception:  # noqa: BLE001 - not listening (yet), or not this device
                    continue
            if node is None and pairing:
                time.sleep(3.0)  # waiting for `pocketnode pair`, which saves new credentials
                continue
            if node is None and self._store_edition():
                # The App Store edition can't be launched from the Mac (devicectl only starts
                # apps signed for development): the person opens it.
                if self.status.startswith(("no phone", "waiting", "phone attached", "connected to")):
                    self.status = OPEN_APP  # (a refusal, "paused" say, keeps its own words)
                time.sleep(1.0)
                continue
            if node is None:
                failures += 1
                if not self.status.startswith(("the phone", "pocketnode")):
                    self.status = "phone attached, app not answering"
                if self.relaunch and failures >= 2 and time.monotonic() - self._last_launch > 30:
                    self._last_launch = time.monotonic()
                    self._launches += 1
                    # Launching only brings a running app to the front. If it has
                    # been launched twice and still doesn't answer, it is running
                    # but deaf: start it afresh.
                    why = launch_app(usb[0].serial, restart=self._launches >= 3)
                    if why:
                        # Launching again would only put the same alert on the
                        # phone's screen; this one is waiting for the user.
                        self.relaunch = False
                        self.status = why
                        ex._note(("launch", usb[0].serial), f"pocketnode: {why}")
                time.sleep(1.0)
                continue
            failures = 0
            self._launches = 0
            self.status = f"connected to {node.name}"
            while node.connected and not self._stop:
                time.sleep(0.3)

    def _store_edition(self) -> bool:
        """Is the phone here the App Store edition? Seen in a hello, or this Mac has store pairings
        and never built the sideloaded app."""
        if self.store_seen:
            return True
        from .pairing import load
        if not load():
            return False
        cfg = app_config()
        return not any(k in cfg for k in ("team", "profile_expires", "bundle_ids"))

    def stop(self):
        self._stop = True


def launch_app(udid: str, restart: bool = False) -> str | None:
    """Start (or foreground) the pocketnode app on the phone over USB.

    Returns None if it launched or may launch on a later try, or the reason it
    can't until the user does something on the phone.
    """
    try:
        r = subprocess.run(["xcrun", "devicectl", "device", "process", "launch",
                            *(["--terminate-existing"] if restart else []), "--device", udid,
                            bundle_id("iphoneos")], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            log.info("pocketnode: launched the app on the phone")
            return None
        kind, why = launch_failure(r.stdout + r.stderr)
        if kind == "locked":
            log.info("pocketnode: the phone is locked, so the app can't be relaunched; unlock it")
            return None  # nothing shows on the phone for this one: keep trying
        if kind == "other":
            log.debug("devicectl launch failed: %s", (r.stdout + r.stderr).strip()[-300:])
        return why
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("devicectl unavailable: %s", e)
    return None


TRUST_STEPS = ("on the phone open Settings > General > VPN & Device Management, pick your Apple ID under "
               "Developer App, tap Trust, then open the pocketnode app")


def launch_failure(out: str) -> tuple[str, str | None]:
    """Why `devicectl device process launch` failed, from its output: (kind, what to tell the user).
    kind is untrusted, locked, expired, missing or other."""
    if "not been explicitly trusted" in out:
        return "untrusted", f"the phone won't run the app until you trust the developer once: {TRUST_STEPS}"
    if "Locked" in out or "could not be, unlocked" in out:
        return "locked", "the phone is locked; unlock it"
    if "invalid code signature" in out or "expired" in out.lower():
        return "expired", "the phone app's signing has expired; reinstall it with `pocketnode app install`"
    if "is not installed" in out or "NotFound" in out:
        return "missing", "the pocketnode app isn't on the phone; install it with `pocketnode app install`"
    return "other", None
