"""Build, install and launch the pocketnode iOS app (device or simulator)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from ..sources import BUNDLE_ID, CONFIG_DIR, pairing_token
from . import stage as stage_mod

IOS_DIR = Path(__file__).resolve().parents[3] / "ios"
DEFAULT_PACKAGES = ["numpy"]
HISTORY = CONFIG_DIR / "build-history.jsonl"


def _run(cmd: list[str], log: Path | None = None, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if log is not None:
        log.write_text(r.stdout + r.stderr)
    return r


def config() -> dict:
    f = CONFIG_DIR / "app.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_config(**kw) -> dict:
    c = config()
    c.update(kw)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "app.json").write_text(json.dumps(c, indent=1))
    return c


def packages() -> list[str]:
    return config().get("packages", DEFAULT_PACKAGES)


def build(sdk: str = "iphonesimulator", pkgs: list[str] | None = None, team: str | None = None,
          bundle_id: str | None = None, restage: bool | str = True) -> Path:
    """Stage packages, generate the Xcode project and build. Returns the .app path.

    restage: True restages everything, "pocketnode" refreshes only pocketnode
    itself (fast while developing), False reuses the existing stage.
    """
    pairing_token()
    cfg = config()
    pkgs = pkgs or packages()
    team = team if team is not None else cfg.get("team", "")
    if not team and sdk == "iphoneos":
        team = detect_team()
        save_config(team=team)
    bundle_id = bundle_id or cfg.get("bundle_id", BUNDLE_ID)
    t0 = time.time()
    if restage == "pocketnode":
        stage_mod.refresh_pocketnode(sdk)
    elif restage:
        rep = stage_mod.stage(pkgs, sdk, bundle_id=bundle_id)
        for mod, why in rep.excluded.items():
            print(f"  left out {mod}: {why}")
    t1 = time.time()
    (CONFIG_DIR / "app-settings.json").write_text(json.dumps({"wifi": bool(cfg.get("wifi")), "torch_mps": bool(cfg.get("torch_mps"))}))
    env = {**os.environ, "POCKETNODE_TEAM": team, "POCKETNODE_BUNDLE_ID": bundle_id}
    r = _run(["xcodegen", "generate", "-q"], cwd=IOS_DIR, env=env)
    if r.returncode:
        raise RuntimeError(r.stderr)
    dest = "generic/platform=iOS Simulator" if sdk == "iphonesimulator" else "generic/platform=iOS"
    if sdk == "iphoneos":
        # Naming the connected phone lets Xcode register it with the team, which
        # a new (free) team needs before it can issue a provisioning profile.
        try:
            udid = device_udid()
            dest = f"id={udid}"
        except RuntimeError:
            udid = None
        if udid:
            r = _run(["devmodectl", "list"])
            if any(udid in l and "disabled" in l for l in r.stdout.splitlines()):
                raise RuntimeError(
                    "Developer Mode is off on the iPhone. Turn it on once: Settings > Privacy & Security > "
                    "Developer Mode, restart when asked, then confirm with your passcode. Then run this again.")
    cmd = ["xcodebuild", "-project", "PocketNode.xcodeproj", "-scheme", "PocketNode", "-sdk", sdk,
           "-destination", dest, "-derivedDataPath", "build", "-configuration", "Debug", "build"]
    if sdk == "iphonesimulator":
        cmd += ["CODE_SIGN_IDENTITY=-"]
    else:
        cmd += ["-allowProvisioningUpdates", "-allowProvisioningDeviceRegistration"]
    log = IOS_DIR / "build" / f"{sdk}.log"
    log.parent.mkdir(exist_ok=True)
    r = _run(cmd, log=log, cwd=IOS_DIR, env=env)
    if r.returncode:
        errs = [l for l in (r.stdout + r.stderr).splitlines() if "error:" in l][:10]
        raise RuntimeError("xcodebuild failed:\n" + "\n".join(errs) + f"\n(full log: {log})")
    t2 = time.time()
    _record({"event": "build", "sdk": sdk, "packages": pkgs, "stage_s": t1 - t0, "build_s": t2 - t1})
    product = "Debug-iphonesimulator" if sdk == "iphonesimulator" else "Debug-iphoneos"
    out = IOS_DIR / "build" / "Build" / "Products" / product / "PocketNode.app"
    if sdk == "iphoneos":
        exp = profile_expiry(out)
        if exp:
            save_config(profile_expires=exp)
    return out


def profile_expiry(app: Path) -> str | None:
    """When the app's provisioning profile stops working (ISO date). Free Apple
    accounts get 7 days; after that the app won't launch until it's reinstalled."""
    import plistlib
    prof = app / "embedded.mobileprovision"
    if not prof.exists():
        return None
    r = subprocess.run(["security", "cms", "-D", "-i", str(prof)], capture_output=True)
    try:
        return plistlib.loads(r.stdout)["ExpirationDate"].isoformat()
    except Exception:  # noqa: BLE001
        return None


def detect_team() -> str:
    """The development team of the Apple ID signed in to Xcode.

    Xcode caches each account's teams in its preferences once the account has
    been looked at in Settings > Apple Accounts. With several teams, pass --team.
    """
    import plistlib
    r = subprocess.run(["defaults", "export", "com.apple.dt.Xcode", "-"], capture_output=True)
    try:
        prefs = plistlib.loads(r.stdout)
    except Exception:  # noqa: BLE001
        prefs = {}
    teams = {t["teamID"]: t.get("teamName", "") for ts in prefs.get("IDEProvisioningTeamByIdentifier", {}).values()
             for t in ts if t.get("teamID")}
    if len(teams) == 1:
        return next(iter(teams))
    if teams:
        raise RuntimeError("several Apple development teams are available; pick one with --team: "
                           + ", ".join(f"{k} ({v})" for k, v in teams.items()))
    accounts = prefs.get("DVTDeveloperAccountManagerAppleIDLists", {}).get("IDE.Identifiers.Prod", [])
    if accounts:
        raise RuntimeError("Xcode has your Apple ID but hasn't loaded its team yet: open Xcode > Settings > "
                           "Apple Accounts and click the account once, then run this again")
    raise RuntimeError("sign in to Xcode first (Xcode > Settings > Apple Accounts > Sign In; a free Apple ID works)")


def simulator() -> str:
    """UDID of the preferred available iPhone simulator (iPhone 18 Pro if present)."""
    r = _run(["xcrun", "simctl", "list", "devices", "available", "-j"])
    devs = [d for rt, ds in json.loads(r.stdout)["devices"].items() if "iOS" in rt for d in ds
            if d["name"].startswith("iPhone")]
    if not devs:
        raise RuntimeError("no iPhone simulator available (Xcode > Settings > Components)")
    devs.sort(key=lambda d: (d["state"] != "Booted", "18 Pro" not in d["name"], d["name"]))
    return devs[0]["udid"]


def install_sim(app: Path, device: str | None = None, launch: bool = True) -> None:
    device = device or simulator()
    _run(["xcrun", "simctl", "boot", device])
    _run(["xcrun", "simctl", "bootstatus", device, "-b"])
    bid = config().get("bundle_id", BUNDLE_ID)
    _run(["xcrun", "simctl", "terminate", device, bid])
    r = _run(["xcrun", "simctl", "install", device, str(app)])
    if r.returncode:
        raise RuntimeError(r.stderr)
    if launch:
        _run(["xcrun", "simctl", "launch", device, bid])


def install_device(app: Path, udid: str | None = None, launch: bool = True) -> None:
    udid = udid or device_udid()
    t0 = time.time()
    r = _run(["xcrun", "devicectl", "device", "install", "app", "--device", udid, str(app)])
    if r.returncode:
        raise RuntimeError(r.stderr or r.stdout)
    _record({"event": "install", "seconds": time.time() - t0})
    if launch:
        bid = config().get("bundle_id", BUNDLE_ID)
        r = _run(["xcrun", "devicectl", "device", "process", "launch", "--device", udid, bid])
        if r.returncode:
            out = r.stdout + r.stderr
            if "not been explicitly trusted" in out:
                raise RuntimeError(
                    "installed, but iOS won't run it until you trust the developer once: on the phone, "
                    "Settings > General > VPN & Device Management > your Apple ID > Trust. Then run "
                    "`pocketnode app launch`.")
            if "Locked" in out or "locked" in out:
                raise RuntimeError("installed; unlock the phone and run `pocketnode app launch`")
            raise RuntimeError("installed, but launching failed: " + (out.strip().splitlines() or ["?"])[-1])


def device_udid() -> str:
    r = _run(["xcrun", "devicectl", "list", "devices"])
    for line in r.stdout.splitlines():
        if "iPhone" in line and "simulated" not in line and "unavailable" not in line:
            m = re.search(r"([0-9A-F]{8}-[0-9A-F]{16})", line)
            if m:
                return m.group(1)
    raise RuntimeError("no iPhone connected (plug it in and unlock it)")


def _record(entry: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    entry["ts"] = time.time()
    with open(HISTORY, "a") as f:
        f.write(json.dumps(entry) + "\n")
