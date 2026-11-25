"""`pocketnode setup`: from a fresh install to a passing acceptance check.

    pocketnode setup          an iPhone over USB
    pocketnode setup --sim    the iOS simulator (no phone, no Apple ID)

Each step checks one thing and prints one line. When something needs doing,
it says exactly what and waits, checking again every few seconds, until it's
done; only problems that need a different Python or a rerun stop it. Running
it again is safe: finished steps pass straight through and the app is rebuilt
incrementally.

The checks are small functions returning Check(ok, message, fix), with the
command output they parse passed in, so they can be tested without a Mac's
tools or a phone.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import Callable, NamedTuple

from .. import progress
from ..sources import PHONE_PORT, TRUST_STEPS, launch_failure
from . import app as app_mod
from . import tools

TESTED_XCODE = "27.1"
MIN_XCODE = 26  # the app targets iOS 26
SIM_ADDRESS = f"127.0.0.1:{PHONE_PORT}"


class Check(NamedTuple):
    ok: bool
    message: str
    fix: str = ""


class Stop(Exception):
    """A problem only a rerun (or a different Python) can pick up."""


def run_cmd(cmd: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{' '.join(cmd)} timed out")


def _last_line(r: subprocess.CompletedProcess) -> str:
    lines = (r.stdout + r.stderr).strip().splitlines()
    return lines[-1] if lines else f"exit status {r.returncode}"


# ------------------------------------------------------------------ checks

def check_platform(system: str | None = None, machine: str | None = None,
                   version: tuple | None = None) -> Check:
    system = system or platform.system()
    machine = machine or platform.machine()
    version = tuple(version or sys.version_info[:3])
    if system != "Darwin":
        return Check(False, f"this is {system}", "pocketnode needs a Mac with Apple silicon (M1 or later) and Xcode")
    if machine != "arm64":
        return Check(False, f"this Python is {machine}",
                     "pocketnode needs an Apple-silicon Mac and an arm64 Python: the phone runs the Mac's own arm64 "
                     "binaries. On an Apple-silicon Mac this means the Python runs under Rosetta; use an arm64 "
                     "Python 3.13 (for example `uv venv --python 3.13`).")
    v = ".".join(map(str, version))
    if version[:2] != (3, 13):
        return Check(False, f"Python {v}",
                     "the phone runs Python 3.13 and the Mac's Python must be the same minor version. Install "
                     "pocketnode into a Python 3.13 environment (for example `uv venv --python 3.13`, then "
                     "`uv pip install pocketnode`) and run `pocketnode setup` from there.")
    return Check(True, f"Apple-silicon Mac, Python {v} ({sys.prefix})")


def check_xcode(run=run_cmd) -> Check:
    r = run(["xcode-select", "-p"])
    path = r.stdout.strip()
    if r.returncode or not path:
        return Check(False, "Xcode isn't installed or selected",
                     "install Xcode from the App Store, open it once, then run "
                     "`sudo xcode-select -s /Applications/Xcode.app`")
    if "CommandLineTools" in path:
        return Check(False, f"only the Command Line Tools are selected ({path})",
                     "the Command Line Tools can't build iOS apps. Install Xcode from the App Store, then run "
                     "`sudo xcode-select -s /Applications/Xcode.app`")
    r = run(["xcodebuild", "-version"])
    out = r.stdout + r.stderr
    if r.returncode:
        if "license" in out.lower():
            return Check(False, "Xcode's licence hasn't been accepted",
                         "run `sudo xcodebuild -license accept`, or open Xcode once and agree")
        return Check(False, f"xcodebuild doesn't run: {_last_line(r)}",
                     "open Xcode once so it can finish installing, then try again")
    m = re.search(r"Xcode (\d+)(?:\.(\d+))?(?:\.\d+)?", out)
    ver = m.group(0).split()[1] if m else "?"
    if m and int(m.group(1)) < MIN_XCODE:
        return Check(False, f"Xcode {ver} is too old",
                     f"the app targets iOS 26, so it needs Xcode {MIN_XCODE} or later (tested with Xcode "
                     f"{TESTED_XCODE}). Update Xcode from the App Store.")
    r = run(["xcodebuild", "-checkFirstLaunchStatus"])
    if r.returncode:
        return Check(False, "Xcode hasn't finished its first launch",
                     "run `sudo xcodebuild -runFirstLaunch`, or open Xcode once and let it install its components")
    return Check(True, f"Xcode {ver} ({path})")


def check_ios_sdk(run=run_cmd) -> Check:
    r = run(["xcrun", "--sdk", "iphoneos", "--show-sdk-version"])
    ver = r.stdout.strip()
    if r.returncode or not re.match(r"^\d+(\.\d+)*$", ver):
        return Check(False, "Xcode has no iOS platform",
                     "run `xcodebuild -downloadPlatform iOS` (several GB), or install it in Xcode > Settings > "
                     "Components")
    return Check(True, f"iOS {ver} SDK")


def check_metal(run=run_cmd) -> Check:
    ok, line = tools.metal_toolchain(run)
    if ok:
        return Check(True, line)
    return Check(False, "Xcode's Metal toolchain isn't installed",
                 f"run `xcodebuild -downloadComponent MetalToolchain` (about {tools.METAL_TOOLCHAIN_MB} MB)")


def check_signing(teams: dict[str, str], signed_in: bool, team: str | None = None) -> Check:
    if team:
        name = teams.get(team, "")
        if teams and team not in teams:
            return Check(False, f"Xcode has no team {team}",
                         "teams Xcode has: " + ", ".join(f"{k} ({v})" for k, v in teams.items()))
        return Check(True, f"team {team}" + (f" ({name})" if name else ""))
    try:
        team = app_mod.pick_team(teams, signed_in)
    except RuntimeError as e:
        return Check(False, "no signing team" if not teams else "several signing teams", str(e))
    name = teams[team]
    kind = ("; a free account: the app is signed for 7 days at a time and iOS asks you to trust the "
            "developer once" if "personal team" in name.lower() else "")
    return Check(True, f"team {team} ({name}){kind}")


def check_phone(devices_text: str) -> tuple[Check, str | None]:
    """(check, UDID of the phone to use) from `xcrun devicectl list devices` output."""
    devs = app_mod.parse_devices(devices_text)
    ready = [d for d in devs if d["available"] and d["paired"]]
    if ready:
        more = f" (using the first of {len(ready)})" if len(ready) > 1 else ""
        return Check(True, f"iPhone {ready[0]['udid']}{more}"), ready[0]["udid"]
    if any(d["available"] for d in devs):
        d = next(d for d in devs if d["available"])
        return Check(False, "the iPhone isn't paired with this Mac",
                     "unlock the phone and tap Trust when it asks whether to trust this computer"), d["udid"]
    return Check(False, "no iPhone connected",
                 "plug the iPhone into this Mac with a cable and unlock it (tap Trust if it asks)"), None


def check_devmode(devmodectl_text: str, udid: str) -> Check:
    state = app_mod.developer_mode(devmodectl_text, udid)
    if state is False:
        return Check(False, "Developer Mode is off",
                     "on the phone open Settings > Privacy & Security > Developer Mode, turn it on, let the phone "
                     "restart, then confirm with your passcode. The Mac can't do this for a phone with a passcode. "
                     "If Developer Mode isn't in that list, leave the phone plugged in: it appears once Xcode has "
                     "seen the phone.")
    if state is None:
        return Check(True, "on (or not reported; the build will say if it isn't)")
    return Check(True, "on")


def parse_lock_state(text: str) -> bool | None:
    """True if `devicectl device info lockState --json-output -` says the phone is locked, None if unknown."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    found: dict[str, bool] = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, bool):
                    found[k.lower()] = v
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    for k, v in found.items():
        if "passcoderequired" in k:
            return v
    for k, v in found.items():
        if k in ("locked", "islocked"):
            return v
    for k, v in found.items():
        if "unlockedsinceboot" in k and v is False:
            return True
    return None


def check_unlocked(lock_json: str) -> Check:
    locked = parse_lock_state(lock_json)
    if locked:
        return Check(False, "the phone is locked",
                     "unlock it, and keep it unlocked until the app is installed and running")
    return Check(True, "unlocked" if locked is False else "lock state not reported; continuing")


# ------------------------------------------------------------- launching

def app_answers(udid: str | None) -> bool:
    """Whether the pocketnode app on the phone accepts a connection on its USB port."""
    from .. import usbmux
    try:
        devs = [d for d in usbmux.list_devices() if udid in (None, d.serial)]
    except OSError:
        return False
    for d in devs:
        try:
            usbmux.connect(d.id, PHONE_PORT).close()
            return True
        except OSError:
            continue
    return False


def address_answers(addr: str = SIM_ADDRESS) -> bool:
    host, port = addr.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=2).close()
        return True
    except OSError:
        return False


def first_launch(launch: Callable[[], subprocess.CompletedProcess], answers: Callable[[], bool],
                 say: Callable[[str], None], poll: float = 3.0, sleep=time.sleep,
                 started_wait: float = 60.0, clock=time.monotonic) -> Check:
    """Start the app once, then wait until it answers.

    Never launches a second time: each failed launch puts an alert on the
    phone's screen. If iOS refuses (an untrusted developer, a locked phone),
    the user is told what to do on the phone, including opening the app, and
    this waits for the app to answer.
    """
    if answers():
        return Check(True, "the app is running")
    r = launch()
    if r.returncode == 0:
        end = clock() + started_wait
        with progress.step("  waiting for the app to start") as p:
            while clock() < end:
                if answers():
                    return Check(True, "started; the app answers")
                sleep(poll)
            p.finish("no answer")
        say("  the app started but isn't answering yet. If it isn't on the phone's screen, open the pocketnode "
            "app on the phone.")
    else:
        kind, why = launch_failure(r.stdout + r.stderr)
        if kind == "untrusted":
            say("  iOS won't run apps from a new developer until you trust it, once:")
            say(f"  {TRUST_STEPS}.")
        elif kind == "locked":
            say("  the phone is locked: unlock it and open the pocketnode app")
        elif why:
            say(f"  {why}")
            if kind in ("expired", "missing"):
                return Check(False, why, "run `pocketnode setup` again")
        else:
            say(f"  the app didn't start ({_last_line(r)}). Open the pocketnode app on the phone.")
    say("  waiting for the app to answer (Ctrl-C to stop; `pocketnode setup` picks up where it left off)")
    with progress.step("  waiting for the app to answer"):
        while not answers():
            sleep(poll)
    return Check(True, "the app answers")


# ------------------------------------------------------------- the flow

class Setup:
    def __init__(self, sim: bool = False, yes: bool = False, team: str | None = None,
                 out: Callable[[str], None] = print, interval: float = 3.0):
        self.sim, self.yes, self.team = sim, yes, team
        self.out, self.interval = out, interval

    def say(self, text: str) -> None:
        self.out(text)

    def step(self, label: str, check: Callable[[], Check], wait: bool = True,
             poll: Callable[[], Check] | None = None, waiting: str | None = None) -> Check:
        """Print `label: result`. On a problem print the fix, then wait for `poll` (default: the check)
        to pass, or stop when waiting can't help. waiting: what the wait is for, in words
        ("the phone to be unlocked"; default: the label)."""
        c = check()
        self.say(f"{label}: {c.message}")
        if c.ok:
            return c
        self.say(f"  to fix: {c.fix}")
        if not wait:
            raise Stop(c.fix)
        self.say("  waiting for that (Ctrl-C to stop; run `pocketnode setup` again any time)")
        poll = poll or check
        last = c.message
        with progress.step(f"  waiting for {waiting or label}") as p:
            while True:
                time.sleep(self.interval)
                c = poll()
                if c.ok:
                    break
                if c.message != last:  # something changed, and needs something else now
                    p.echo(f"  now: {c.message}. To fix: {c.fix}", self.say)
                    last = c.message
        self.say(f"  done: {c.message}")
        return c

    def confirm(self, question: str) -> bool:
        if self.yes or not sys.stdin.isatty():
            return True
        return input(f"  {question} [Y/n] ").strip().lower() in ("", "y", "yes")

    # ---------------------------------------------------------------- steps

    def toolchain(self) -> None:
        self.step("Mac and Python", check_platform, wait=False)
        if "/uv/tools/" in sys.prefix:
            self.say("  note: this is a `uv tool` environment, so the phone gets only its packages. Install "
                     "pocketnode into the environment you work in instead (`uv pip install pocketnode`).")
        self.step("Xcode", check_xcode)
        sdk = check_ios_sdk()
        if not sdk.ok:
            self.say(f"iOS platform: {sdk.message}")
            if self.confirm("download it now with `xcodebuild -downloadPlatform iOS` (several GB)?"):
                subprocess.run(["xcodebuild", "-downloadPlatform", "iOS"])
        self.step("iOS platform", check_ios_sdk, waiting="the iOS platform")
        self.step("XcodeGen", self._xcodegen, poll=self._xcodegen_on_path)
        self.step("embedded Python", self._python, wait=False)
        self._metal()

    def _xcodegen(self) -> Check:
        try:
            exe = tools.xcodegen()
        except tools.ToolError as e:
            return Check(False, f"not on PATH, and fetching XcodeGen {tools.XCODEGEN['version']} failed ({e})",
                         "install it with `brew install xcodegen`")
        r = run_cmd([exe, "--version"])
        return Check(r.returncode == 0, f"{r.stdout.strip() or _last_line(r)} ({exe})",
                     "install it with `brew install xcodegen`")

    def _xcodegen_on_path(self) -> Check:
        return self._xcodegen() if shutil.which("xcodegen") else \
            Check(False, "not on PATH yet", "install it with `brew install xcodegen`")

    def _python(self) -> Check:
        p = tools.PYTHON_SUPPORT
        if not (tools.CACHE / f"py{p['python'].replace('.', '')}-ios" / "Python.xcframework").exists():
            self.say(f"  downloading Python {p['python']} for iOS ({p['build']}, {p['mb']} MB) from BeeWare's "
                     "Python-Apple-support")
        try:
            xcf = tools.python_xcframework()
        except tools.ToolError as e:
            return Check(False, str(e), "check the network connection and run `pocketnode setup` again")
        return Check(True, f"Python {p['python']} ({p['build']}) at {xcf}")

    def _metal(self) -> None:
        c = check_metal()
        if c.ok:
            self.say(f"Metal toolchain: {c.message}")
            return
        self.say(f"Metal toolchain: {c.message}. It compiles PyTorch's and MLX's GPU shaders for iOS.")
        if self.confirm(f"download it now (about {tools.METAL_TOOLCHAIN_MB} MB)?"):
            tools.install_metal_toolchain()
            c = check_metal()
        if c.ok:
            self.say(f"  done: {c.message}")
        else:
            self.say("  continuing without it: PyTorch's GPU backend and MLX on the phone will be limited. "
                     "Install it later with `xcodebuild -downloadComponent MetalToolchain`, then run "
                     "`pocketnode app install`.")

    def signing(self) -> str:
        self.step("Apple ID and team", lambda: check_signing(*app_mod.xcode_teams(), team=self.team),
                  waiting="the Apple ID and team")
        team = self.team or app_mod.detect_team()
        app_mod.save_config(team=team)
        return team

    def phone(self) -> str:
        udid: list[str | None] = [None]

        def connected() -> Check:
            c, udid[0] = check_phone(run_cmd(["xcrun", "devicectl", "list", "devices"]).stdout)
            return c

        self.step("iPhone", connected, waiting="the iPhone")
        u = udid[0]
        self.step("Developer Mode", lambda: check_devmode(run_cmd(["devmodectl", "list"]).stdout, u))
        self.step("phone unlocked", lambda: check_unlocked(run_cmd(
            ["xcrun", "devicectl", "device", "info", "lockState", "--device", u, "--quiet",
             "--json-output", "-"], timeout=60).stdout), waiting="the phone to be unlocked")
        return u

    def build_install(self, sdk: str, udid: str | None) -> None:
        # What makes a build slow is an empty build directory for this SDK; the
        # steps below show their own progress, so only a cold build gets a warning.
        cold = not app_mod.product(sdk).exists()
        self.say("building the app" + (": the first build takes several minutes" if cold else ""))
        try:
            p = app_mod.build(sdk, flavour="dev")  # setup is for the sideloaded app, whatever `app --store` saved
        except (RuntimeError, LookupError) as e:
            self.say(f"  the build failed: {e}")
            raise Stop("fix that and run `pocketnode setup` again") from None
        try:
            if sdk == "iphonesimulator":
                app_mod.install_sim(p)
            else:
                app_mod.install_device(p, udid, launch=False)
        except RuntimeError as e:
            self.say(f"  installing failed: {e}")
            raise Stop("fix that and run `pocketnode setup` again") from None

    def launch(self, udid: str) -> None:
        self.say("starting the app")
        bid = app_mod.bundle_id("iphoneos")
        c = first_launch(
            lambda: run_cmd(["xcrun", "devicectl", "device", "process", "launch", "--device", udid, bid]),
            lambda: app_answers(udid), self.say, poll=self.interval)
        self.say(f"  {c.message}")
        if not c.ok:
            raise Stop(c.fix)

    def acceptance(self, address: str | None) -> int:
        import pocketnode as pn

        from .. import check
        self.say("acceptance check (quick): every feature against the "
                 + ("simulator" if address else "phone") + ", compared with this Mac")
        ex = pn.connect(*([address] if address else []), mac_workers=2, phone=not address, notes=False)
        try:
            if not ex.wait_for("phone", 120):
                src = next((s for s in ex.sources if hasattr(s, "status")), None)
                self.say(f"  the app didn't connect ({src.status if src else 'not reachable'})")
                return 1
            ex.wait_for("mac", 30)
            return check.main(ex, quick=True)
        finally:
            ex.shutdown(wait=False)

    def done(self) -> None:
        conn = f'pn.connect("{SIM_ADDRESS}")' if self.sim else "pn.connect()"
        self.say("")
        self.say("You're set. Things to try:")
        self.say("  pocketnode status                      what's connected, and the link")
        self.say(f"  python -c 'import pocketnode as pn; ex = {conn}; "
                 "f = ex.submit(sum, range(10)); print(f.result(), f.placement)'")
        self.say("  with pn.parallel(): GridSearchCV(..., n_jobs=-1).fit(X, y)   (scikit-learn on Mac and phone)")
        self.say("  %load_ext pocketnode, then a %%phone cell                       (Jupyter)")
        if not self.sim:
            self.say("  pocketnode packages add lightgbm       put another compiled package on the phone")

    def run(self) -> int:
        try:
            self.toolchain()
            if self.sim:
                self.say("Apple ID and team: not needed for the simulator")
                self.step("simulator", _simulator_check, waiting="an iPhone simulator")
                self.build_install("iphonesimulator", None)
                self.say("starting the app in the simulator")
                end = time.monotonic() + 120
                with progress.step("  waiting for the app to answer"):
                    while not address_answers() and time.monotonic() < end:
                        time.sleep(1)
                code = self.acceptance(SIM_ADDRESS)
            else:
                self.signing()
                udid = self.phone()
                self.build_install("iphoneos", udid)
                self.launch(udid)
                code = self.acceptance(None)
        except Stop as e:
            self.say(f"stopped: {e}")
            return 1
        except KeyboardInterrupt:
            self.say("\nstopped. Run `pocketnode setup` again when ready; it picks up where it left off.")
            return 130
        if code == 0:
            self.done()
        else:
            self.say("Some items failed. `pocketnode check` runs the full check again; the README's "
                     "troubleshooting section covers the common causes.")
        return code


def _simulator_check() -> Check:
    try:
        return Check(True, f"iPhone simulator {app_mod.simulator()}")
    except (RuntimeError, ValueError, KeyError) as e:
        return Check(False, str(e), "install an iOS simulator runtime in Xcode > Settings > Components")


def main(sim: bool = False, yes: bool = False, team: str | None = None) -> int:
    return Setup(sim=sim, yes=yes, team=team).run()
