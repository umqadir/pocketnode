"""Pairing at run time, for the App Store edition.

The sideloaded app has a pairing token baked in when it is built. A store build
is the same for everyone, so instead the phone gives each Mac its own secret
when the person pairs them:

1. On the phone they tap Pair a Mac; it shows a six-digit code for two minutes.
2. `pocketnode pair` on the Mac (over the USB cable) proves it knows the code.
3. The phone asks "Allow <Mac> to run Python code on this iPhone?". On Allow it
   sends that Mac a random 256-bit token and proves it knew the code too.

Later sessions prove knowledge of the token without sending it, exactly as the
sideloaded edition does, looked up by the Mac's pair id. Forgetting a Mac on
the phone deletes its token, so its next connection is refused.

Phone side: PairStore (the tokens, in the keychain on iOS) and PairingWindow
(one open "Pair a Mac" window). Mac side: the credentials file and pair_phone().
"""

from __future__ import annotations

import ctypes
import hmac
import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path

WINDOW_SECONDS = 120      # how long a pairing code is good for
MAX_WRONG = 5             # wrong codes before the window closes
DECIDE_SECONDS = 60       # how long the phone waits for Allow / Don't Allow
ANSWER_SECONDS = 90       # how long the Mac waits for the phone's answer


class NotPaired(ConnectionError):
    """This Mac has no credentials for that phone."""


class PairingRevoked(PermissionError):
    """The phone no longer accepts this Mac's credentials (it was forgotten there)."""


class PairingFailed(RuntimeError):
    """Pairing didn't complete. `why` is the reason the phone gave (or a local one)."""

    def __init__(self, why: str, text: str | None = None):
        super().__init__(text or FAIL_TEXT.get(why, f"pairing failed ({why})"))
        self.why = why


FAIL_TEXT = {
    "not_pairing": "the iPhone isn't waiting to pair: in the pocketnode app tap Pair a Mac, "
                   "then run `pocketnode pair` again while the code is showing",
    "code": "that code doesn't match the one on the iPhone (five wrong codes end the attempt; "
            "then tap Pair a Mac again for a new code)",
    "usb_only": "the iPhone only pairs over the USB cable: connect it with a cable and run "
                "`pocketnode pair` without --address",
    "denied": "the iPhone's owner tapped Don't Allow, so this Mac wasn't paired",
    "timeout": "nobody tapped Allow on the iPhone within a minute; run `pocketnode pair` again",
    "proof": "the iPhone's answer didn't prove it knew the code, so nothing was saved; "
             "try again, and check that the code came from your own iPhone",
    "no_phone": "no iPhone running the App Store edition of pocketnode answered over USB: "
                "connect it with a cable, open the app and tap Pair a Mac",
    "not_store": "that phone runs the sideloaded edition of pocketnode, which is paired when "
                 "the app is built; there is nothing to pair",
    "no_answer": "the iPhone closed the connection without answering",
}


def hm(key: str, msg: str) -> str:
    """HMAC-SHA256, hex. Key and message are utf-8."""
    return hmac.new(key.encode(), msg.encode(), "sha256").hexdigest()


# ------------------------------------------------------------ phone: secrets

class FileBackend:
    """The pairing record in a file (tests, the stand-in, a fallback on the phone)."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self) -> str | None:
        try:
            return self.path.read_text()
        except FileNotFoundError:
            return None

    def write(self, text: str) -> None:
        _write_private(self.path, text)


class KeychainBackend:
    """The pairing record in the iOS keychain, through C functions the host app exports."""

    KEY = b"paired"

    def __init__(self):
        lib = ctypes.CDLL(None)
        self._set = lib.pocketnode_secret_set
        self._get = lib.pocketnode_secret_get
        self._del = lib.pocketnode_secret_delete
        self._set.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        self._get.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        self._del.argtypes = [ctypes.c_char_p]
        for f in (self._set, self._get, self._del):
            f.restype = ctypes.c_int

    @classmethod
    def available(cls) -> bool:
        try:
            lib = ctypes.CDLL(None)
            return all(hasattr(lib, n) for n in ("pocketnode_secret_set", "pocketnode_secret_get",
                                                 "pocketnode_secret_delete"))
        except OSError:
            return False

    def read(self) -> str | None:
        cap = 16 << 10
        while cap <= 16 << 20:
            buf = ctypes.create_string_buffer(cap)
            n = self._get(self.KEY, buf, cap)
            if n == -1:
                return None
            if n == -2:
                cap *= 4
                continue
            if n < 0:
                raise OSError(f"reading the pairing record from the keychain failed ({n})")
            return buf.raw[:n].decode()
        raise OSError("the pairing record in the keychain is too large")

    def write(self, text: str) -> None:
        rc = self._set(self.KEY, text.encode())
        if rc != 0:
            raise OSError(f"saving the pairing record in the keychain failed ({rc})")

    def delete(self) -> None:
        self._del(self.KEY)


class PairStore:
    """The Macs this phone is paired with: {"phone_id", "macs": {pair_id: {name, token, paired_at, last_seen}}}."""

    def __init__(self, backend):
        self.backend = backend
        self._lock = threading.RLock()
        try:
            data = json.loads(backend.read() or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("macs", {})
        self._data = data

    def _save(self) -> None:
        self.backend.write(json.dumps(self._data, separators=(",", ":")))

    @property
    def phone_id(self) -> str:
        with self._lock:
            if not self._data.get("phone_id"):
                self._data["phone_id"] = secrets.token_hex(8)
                self._save()
            return self._data["phone_id"]

    def list(self) -> list[dict]:
        """Paired Macs, without their tokens."""
        with self._lock:
            return [{"pair_id": pid, "name": m.get("name", ""), "paired_at": m.get("paired_at"),
                     "last_seen": m.get("last_seen")} for pid, m in self._data["macs"].items()]

    def token(self, pair_id) -> str | None:
        with self._lock:
            m = self._data["macs"].get(str(pair_id)) if pair_id else None
            return m["token"] if m else None

    def name(self, pair_id) -> str | None:
        with self._lock:
            m = self._data["macs"].get(str(pair_id)) if pair_id else None
            return m.get("name") if m else None

    def add(self, name: str) -> tuple[str, str]:
        pair_id, token = secrets.token_hex(8), secrets.token_hex(32)
        now = time.time()
        with self._lock:
            self.phone_id  # noqa: B018 - created with the first pairing at the latest
            self._data["macs"][pair_id] = {"name": str(name)[:200], "token": token, "paired_at": now,
                                           "last_seen": now}
            self._save()
        return pair_id, token

    def forget(self, pair_id) -> bool:
        with self._lock:
            if self._data["macs"].pop(str(pair_id), None) is None:
                return False
            self._save()
            return True

    def touch(self, pair_id) -> None:
        """Note that a Mac connected (written at most once a minute)."""
        now = time.time()
        with self._lock:
            m = self._data["macs"].get(str(pair_id))
            if m is None or now - (m.get("last_seen") or 0) < 60:
                return
            m["last_seen"] = now
            self._save()


def seed_pair_file(path, mac_name: str = "this Mac") -> dict:
    """A pair file holding one paired Mac (for the stand-in). Returns phone_id, pair_id, token."""
    store = PairStore(FileBackend(path))
    pair_id, token = store.add(mac_name)
    return {"phone_id": store.phone_id, "pair_id": pair_id, "token": token}


# ------------------------------------------------------------ phone: window

class PairingWindow:
    """The phone's "Pair a Mac" window: a code, how long it lasts, and the request on screen."""

    def __init__(self, ttl: float = WINDOW_SECONDS, decide_timeout: float = DECIDE_SECONDS,
                 max_wrong: int = MAX_WRONG):
        self.ttl = ttl
        self.decide_timeout = decide_timeout
        self.max_wrong = max_wrong
        self._cv = threading.Condition()
        self._code: str | None = None
        self._expires = 0.0
        self._wrong = 0
        self._request: dict | None = None
        self._answer: bool | None = None
        self._keep_open = False
        self.auto_allow = False  # Debug-build automation and the stand-in: answer Allow by itself
        self.on_change = None    # called (without arguments) when the state changes

    def begin(self, ttl: float | None = None, code: str | None = None, keep_open: bool = False) -> str:
        """Open (or reopen) the window. Returns the code to show."""
        with self._cv:
            if self._request is not None and self._answer is None:
                self._answer = False  # a new window replaces a request still on screen
            self._code = code or f"{secrets.randbelow(10**6):06d}"
            self._keep_open = keep_open
            self._expires = float("inf") if keep_open else time.monotonic() + (ttl or self.ttl)
            self._wrong = 0
            self._cv.notify_all()
            code = self._code
        self._changed()
        return code

    def cancel(self) -> None:
        with self._cv:
            self._close()
            if self._request is not None and self._answer is None:
                self._answer = False
            self._cv.notify_all()
        self._changed()

    def _close(self) -> None:
        self._code, self._expires, self._wrong, self._keep_open = None, 0.0, 0, False

    def _open(self) -> bool:
        if self._code is None:
            return False
        if time.monotonic() >= self._expires:
            self._close()
            return False
        return True

    def state(self) -> dict | None:
        """None, or {"code", "expires_in", "request": None | {"name"}}."""
        with self._cv:
            if not self._open():
                return None
            left = self._expires - time.monotonic()
            return {"code": self._code, "expires_in": None if left == float("inf") else max(0, int(left)),
                    "request": dict(self._request) if self._request and self._answer is None else None}

    def decide(self, allow: bool) -> None:
        """The person's answer to the request on screen."""
        with self._cv:
            if self._request is not None and self._answer is None:
                self._answer = bool(allow)
                self._cv.notify_all()

    def _changed(self) -> None:
        cb = self.on_change
        if cb is not None:
            try:
                cb()
            except Exception:  # noqa: BLE001 - the UI's business
                pass

    def handle(self, conn, msg: dict, nonce: str, store: PairStore, local: bool,
               phone_name: str = "iPhone") -> bool:
        """Answer one {"t": "pair"} frame on `conn`. Returns whether a Mac was paired."""
        why, ok = self._consider(msg, nonce, store, local)
        if why:
            conn.send({"t": "pair_fail", "why": why})
            return False
        code, pair_id, token = ok
        m = str(msg.get("nonce", ""))
        conn.send({"t": "pair_ok", "pair_id": pair_id, "token": token, "phone_id": store.phone_id,
                   "phone_name": phone_name, "proof": hm(code, f"phone:{m}:{nonce}")})
        return True

    def _consider(self, msg, nonce, store, local):
        name = str(msg.get("name") or "a Mac")[:200]
        with self._cv:
            if not self._open() or (self._request is not None and self._answer is None):
                return "not_pairing", None
            if not local:
                return "usb_only", None
            code = self._code
            expect = hm(code, f"pair:{nonce}:{msg.get('nonce', '')}")
            if not hmac.compare_digest(str(msg.get("proof", "")), expect):
                self._wrong += 1
                if self._wrong >= self.max_wrong:
                    self._close()
                    self._cv.notify_all()
                return "code", None
            self._request, self._answer = {"name": name}, (True if self.auto_allow else None)
        self._changed()
        with self._cv:
            end = time.monotonic() + self.decide_timeout
            while self._answer is None:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(left)
            answer, self._request, self._answer = self._answer, None, None
            if answer is not None and not self._keep_open:
                self._close()  # single use: closes on Allow or Don't Allow
            self._cv.notify_all()
        self._changed()
        if answer is None:
            return "timeout", None
        if not answer:
            return "denied", None
        pair_id, token = store.add(name)
        return None, (code, pair_id, token)


# ------------------------------------------------------------ Mac: credentials

def _config_dir() -> Path:
    from .sources import CONFIG_DIR
    return CONFIG_DIR


def creds_path() -> Path:
    return _config_dir() / "paired.json"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load() -> dict:
    """{phone_id: {"pair_id", "token", "phone_name", "paired_at"}}"""
    try:
        d = json.loads(creds_path().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(creds: dict) -> None:
    _write_private(creds_path(), json.dumps(creds, indent=1))


def remember(phone_id: str, pair_id: str, token: str, phone_name: str) -> None:
    d = load()
    d[phone_id] = {"pair_id": pair_id, "token": token, "phone_name": phone_name, "paired_at": time.time()}
    save(d)


def remove(phone_id: str) -> bool:
    d = load()
    if d.pop(phone_id, None) is None:
        return False
    save(d)
    return True


def credentials_for(hello: dict) -> tuple[str, dict] | None:
    """(token, extra auth fields) for the phone that sent `hello`, or None if this Mac isn't paired with it."""
    c = load().get(str(hello.get("phone_id")))
    if not c or not c.get("token"):
        return None
    return c["token"], {"pair_id": c["pair_id"]}


def mac_name() -> str:
    import subprocess
    try:
        r = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname()


def _hello(sock: socket.socket, name: str):
    from .wire import HANDSHAKE_TIMEOUT, Connection
    conn = Connection(sock, name)
    conn.sock.settimeout(HANDSHAKE_TIMEOUT)
    hello, _ = conn.recv()
    if hello.get("t") != "hello":
        conn.close()
        raise PairingFailed("no_answer", f"unexpected greeting from {name}")
    return conn, hello


def _find(address: str | None, udid: str | None) -> tuple[callable, str]:
    """A way to open a fresh connection to the phone that wants pairing, and its description."""
    from .wire import ConnectionClosed
    if address:
        host, port = address.rsplit(":", 1)

        def dial():
            return socket.create_connection((host.strip("[]"), int(port)), timeout=10)

        try:
            conn, hello = _hello(dial(), address)
        except (OSError, ConnectionClosed) as e:
            raise PairingFailed("no_phone", f"nothing answered at {address} ({e})") from None
        conn.close()
        if hello.get("pairing") != "runtime":
            raise PairingFailed("not_store")
        return dial, address
    from . import usbmux
    from .sources import PHONE_PORT
    try:
        devs = [d for d in usbmux.list_devices() if udid in (None, d.serial)]
    except OSError:
        devs = []
    devs = [d for d in devs if d.connection == "USB"]
    dev_seen = False
    for dev in devs:
        def dial(dev=dev):
            return usbmux.connect(dev.id, PHONE_PORT)
        try:
            conn, hello = _hello(dial(), f"usb:{dev.serial}")
        except (OSError, ConnectionClosed, PairingFailed):
            continue
        conn.close()
        if hello.get("pairing") == "runtime":
            return dial, f"usb:{dev.serial}"
        dev_seen = True
    raise PairingFailed("not_store" if dev_seen else "no_phone")


def pair_over(sock: socket.socket, code: str, name: str, where: str = "the iPhone") -> dict:
    """Run the pairing exchange on a fresh connection to the phone and save the credentials."""
    conn, hello = _hello(sock, where)
    try:
        if hello.get("pairing") != "runtime":
            raise PairingFailed("not_store")
        nonce_n = str(hello.get("nonce", ""))
        m = secrets.token_hex(16)
        conn.send({"t": "pair", "name": name, "nonce": m, "proof": hm(code, f"pair:{nonce_n}:{m}")})
        conn.sock.settimeout(ANSWER_SECONDS)
        try:
            ans, _ = conn.recv()
        except (OSError, ConnectionError) as e:
            raise PairingFailed("no_answer") from e
        if ans.get("t") == "pair_fail":
            raise PairingFailed(str(ans.get("why", "unknown")))
        if ans.get("t") != "pair_ok":
            raise PairingFailed("no_answer", f"unexpected answer from the iPhone: {ans.get('t')}")
        if not hmac.compare_digest(str(ans.get("proof", "")), hm(code, f"phone:{m}:{nonce_n}")):
            raise PairingFailed("proof")
        phone_id = str(ans.get("phone_id") or hello.get("phone_id"))
        phone_name = str(ans.get("phone_name") or hello.get("name") or "iPhone")
        remember(phone_id, str(ans["pair_id"]), str(ans["token"]), phone_name)
        return {"phone_id": phone_id, "phone_name": phone_name}
    finally:
        conn.close()


def pair_phone(address: str | None = None, udid: str | None = None, code: str | None = None,
               name: str | None = None, prompt=None) -> dict:
    """Pair this Mac with an iPhone running the App Store edition. Returns {"phone_id", "phone_name"}.

    address: "host:port" instead of the USB cable (the simulator, or the stand-in).
    code: the six digits the phone shows; asked for with `prompt` when not given.
    """
    dial, where = _find(address, udid)
    if code is None:
        ask = prompt or input
        code = ask("Enter the code shown on the iPhone: ")
    code = "".join(str(code).split())
    if not (len(code) == 6 and code.isdigit()):
        raise PairingFailed("code", "the code is the six digits shown on the iPhone")
    try:
        sock = dial()
    except OSError as e:
        raise PairingFailed("no_phone", f"couldn't reach {where} again ({e})") from None
    return pair_over(sock, code, name or mac_name(), where)
