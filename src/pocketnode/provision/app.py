"""Build, install and launch the pocketnode iOS app (device or simulator).

The app's sources ship inside the package (`pocketnode/ios`). Each build copies
them into a working directory under the pocketnode home (~/.pocketnode/ios),
generates the Xcode project there with XcodeGen and builds it, so nothing is
ever written next to the installed package.
"""

from __future__ import annotations

import importlib.metadata as md
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..sources import CONFIG_DIR, pairing_token
from ..sources import bundle_id as configured_bundle_id
from . import stage as stage_mod
from . import tools

SOURCE_DIR = Path(__file__).resolve().parent.parent / "ios"
WORK_DIR = CONFIG_DIR / "ios"
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


def product(sdk: str) -> Path:
    """Where a build for `sdk` puts the app."""
    kind = "Debug-iphonesimulator" if sdk == "iphonesimulator" else "Debug-iphoneos"
    return WORK_DIR / "build" / "Build" / "Products" / kind / "PocketNode.app"


def bundle_id(sdk: str = "iphoneos") -> str:
    return configured_bundle_id(sdk, cfg=config())


def prepare_project(team: str, bundle: str) -> Path:
    """Copy the app sources into the working directory and generate the Xcode project there."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("App", "scripts"):
        shutil.rmtree(WORK_DIR / name, ignore_errors=True)
        shutil.copytree(SOURCE_DIR / name, WORK_DIR / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(SOURCE_DIR / "project.yml", WORK_DIR / "project.yml")
    xcf, link = tools.python_xcframework(), WORK_DIR / "Python.xcframework"
    if link.is_symlink() or link.exists():
        if link.resolve() == xcf.resolve():
            xcf = None
        elif link.is_symlink():
            link.unlink()
        else:
            shutil.rmtree(link)
    if xcf is not None:
        link.symlink_to(xcf)
    env = {**os.environ, "POCKETNODE_TEAM": team, "POCKETNODE_BUNDLE_ID": bundle}
    r = _run([tools.xcodegen(), "generate", "-q"], cwd=WORK_DIR, env=env)
    if r.returncode:
        raise RuntimeError("xcodegen failed: " + (r.stderr or r.stdout).strip())
    return WORK_DIR


def _installed(names: list[str]) -> list[str]:
    """The names installed in this environment; says which aren't (the app is built without them)."""
    out = []
    for n in names:
        try:
            md.distribution(n)
            out.append(n)
        except md.PackageNotFoundError:
            print(f"  {n} isn't installed in this Python environment, so the app is built without it")
    return out


def build(sdk: str = "iphonesimulator", pkgs: list[str] | None = None, team: str | None = None,
          bundle_id: str | None = None, restage: bool | str = True) -> Path:
    """Stage packages, generate the Xcode project and build. Returns the .app path.

    restage: True restages everything, "pocketnode" refreshes only pocketnode
    itself (fast while developing), False reuses the existing stage.
    """
    pairing_token()
    cfg = config()
    pkgs = _installed(pkgs or packages())
    team = team if team is not None else cfg.get("team", "")
    if not team and sdk == "iphoneos":
        team = detect_team()
        save_config(team=team)
    bundle_id = bundle_id or configured_bundle_id(sdk, team, cfg)
    if sdk == "iphoneos" and not cfg.get("bundle_id"):
        # Saved per team, so the app keeps its identifier (and its data) across builds.
        save_config(bundle_ids={**cfg.get("bundle_ids", {}), team: bundle_id})
    t0 = time.time()
    if restage == "pocketnode":
        stage_mod.refresh_pocketnode(sdk)
    elif restage:
        rep = stage_mod.stage(pkgs, sdk, bundle_id=bundle_id)
        for mod, why in rep.excluded.items():
            print(f"  left out {mod}: {why}")
        for w in rep.warnings:
            if "no iOS GPU" in w:
                print(f"  {w}")
    t1 = time.time()
    # torch's Metal backend is on unless a bundled torch couldn't get its iOS
    # shaders (then most GPU operations would fail) or the user turned it off.
    # The simulator's GPU has no memory budget, so it's always off there.
    mps = cfg.get("torch_mps")
    if sdk == "iphonesimulator":
        mps = False
    elif mps is None:
        mps = True
        rep = stage_mod.STAGE_ROOT / sdk / "stage-report.json"
        if rep.exists():
            r = json.loads(rep.read_text())
            if "torch" in r.get("dists", {}) and not r.get("notes", {}).get("torch_metal"):
                mps = False
    (CONFIG_DIR / "app-settings.json").write_text(json.dumps({"wifi": bool(cfg.get("wifi")), "torch_mps": bool(mps)}))
    work = prepare_project(team, bundle_id)
    env = {**os.environ, "POCKETNODE_TEAM": team, "POCKETNODE_BUNDLE_ID": bundle_id,
           "POCKETNODE_HOME": str(CONFIG_DIR), "POCKETNODE_STAGE": str(stage_mod.STAGE_ROOT)}
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
            if developer_mode(r.stdout, udid) is False:
                raise RuntimeError(
                    "Developer Mode is off on the iPhone. Turn it on once: Settings > Privacy & Security > "
                    "Developer Mode, restart when asked, then confirm with your passcode. Then run this again.")
    cmd = ["xcodebuild", "-project", "PocketNode.xcodeproj", "-scheme", "PocketNode", "-sdk", sdk,
           "-destination", dest, "-derivedDataPath", "build", "-configuration", "Debug", "build"]
    if sdk == "iphonesimulator":
        cmd += ["CODE_SIGN_IDENTITY=-"]
    else:
        cmd += ["-allowProvisioningUpdates", "-allowProvisioningDeviceRegistration"]
    log = work / "build" / f"{sdk}.log"
    log.parent.mkdir(exist_ok=True)
    r = _run(cmd, log=log, cwd=work, env=env)
    if r.returncode:
        raise RuntimeError(build_failure(r.stdout + r.stderr, bundle_id, log))
    t2 = time.time()
    _record({"event": "build", "sdk": sdk, "packages": pkgs, "stage_s": t1 - t0, "build_s": t2 - t1})
    out = product(sdk)
    if sdk == "iphoneos":
        exp = profile_expiry(out)
        if exp:
            save_config(profile_expires=exp)
    return out


def build_failure(output: str, bundle: str, log: Path) -> str:
    """A message for a failed xcodebuild: what to do for the failures people hit, else the errors."""
    if "cannot be registered to your development team" in output or \
            re.search(r"identifier .* is not available", output):
        return (f"the bundle identifier {bundle} is taken by another developer's team. Pick your own, e.g. "
                f"`pocketnode app install --bundle-id com.yourname.pocketnode`, and run this again.")
    if "maximum App ID limit" in output or "App ID limit" in output:
        return ("a free Apple ID can register only 10 app identifiers per 7 days, and this one is used up. "
                "Wait for the oldest to expire, or reuse one with `--bundle-id`.")
    if "No Account for Team" in output or "No signing certificate" in output:
        return ("Xcode has no signing account for this team: open Xcode > Settings > Apple Accounts, sign in "
                "(a free Apple ID works) and click the account once so Xcode loads its team.")
    if "Developer Mode disabled" in output or ("developer mode" in output.lower() and "disabled" in output):
        return ("Developer Mode is off on the iPhone: Settings > Privacy & Security > Developer Mode, "
                "restart when asked, then confirm with your passcode.")
    lines = output.splitlines()
    errs = [l for l in lines if "error:" in l][:10]
    if not errs:  # a build script that failed says so in its own words
        errs = [l for l in lines if re.search(r"failed|cannot|not found|no such file", l, re.I)
                and not re.search(r"BUILD FAILED|build commands failed|Command \w+ failed", l)][-5:]
    return "xcodebuild failed:\n" + "\n".join(l.strip()[:300] for l in errs) + f"\n(full log: {log})"


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


def xcode_teams() -> tuple[dict[str, str], bool]:
    """({team ID: name} Xcode has loaded, whether any Apple ID is signed in to Xcode)."""
    import plistlib
    r = subprocess.run(["defaults", "export", "com.apple.dt.Xcode", "-"], capture_output=True)
    try:
        prefs = plistlib.loads(r.stdout)
    except Exception:  # noqa: BLE001
        prefs = {}
    teams = {t["teamID"]: t.get("teamName", "") for ts in prefs.get("IDEProvisioningTeamByIdentifier", {}).values()
             for t in ts if t.get("teamID")}
    accounts = prefs.get("DVTDeveloperAccountManagerAppleIDLists", {}).get("IDE.Identifiers.Prod", [])
    return teams, bool(accounts)


def pick_team(teams: dict[str, str], signed_in: bool) -> str:
    """The one team to sign with, or RuntimeError saying what to do."""
    if len(teams) == 1:
        return next(iter(teams))
    if teams:
        raise RuntimeError("several Apple development teams are available; pick one with "
                           "`pocketnode app install --team TEAMID` (or `pocketnode setup --team TEAMID`): "
                           + ", ".join(f"{k} ({v})" for k, v in teams.items()))
    if signed_in:
        raise RuntimeError("Xcode has your Apple ID but hasn't loaded its team yet: open Xcode > Settings > "
                           "Apple Accounts and click the account once")
    raise RuntimeError("sign in to Xcode: Xcode > Settings > Apple Accounts > Sign In (a free Apple ID works), "
                       "then click the account once so Xcode loads its team")


def detect_team() -> str:
    """The development team of the Apple ID signed in to Xcode.

    Xcode caches each account's teams in its preferences once the account has
    been looked at in Settings > Apple Accounts. With several teams, pass --team.
    """
    return pick_team(*xcode_teams())


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
    bid = bundle_id("iphonesimulator")
    _run(["xcrun", "simctl", "terminate", device, bid])
    r = _run(["xcrun", "simctl", "install", device, str(app)])
    if r.returncode:
        raise RuntimeError(r.stderr)
    if launch:
        _run(["xcrun", "simctl", "launch", device, bid])


def install_device(app: Path, udid: str | None = None, launch: bool = True) -> None:
    from ..sources import launch_failure
    udid = udid or device_udid()
    t0 = time.time()
    r = _run(["xcrun", "devicectl", "device", "install", "app", "--device", udid, str(app)])
    if r.returncode:
        raise RuntimeError(r.stderr or r.stdout)
    _record({"event": "install", "seconds": time.time() - t0})
    if launch:
        r = _run(["xcrun", "devicectl", "device", "process", "launch", "--device", udid, bundle_id("iphoneos")])
        if r.returncode:
            out = r.stdout + r.stderr
            kind, why = launch_failure(out)
            if kind == "untrusted":
                raise RuntimeError(f"installed, but {why}. Or run `pocketnode app launch` after trusting.")
            if kind == "locked":
                raise RuntimeError("installed; unlock the phone and run `pocketnode app launch`")
            raise RuntimeError("installed, but launching failed: " + (out.strip().splitlines() or ["?"])[-1])


_UDID = re.compile(r"\b([0-9A-F]{8}-[0-9A-F]{16})\b")


def parse_devices(text: str) -> list[dict]:
    """Physical iPhones in `xcrun devicectl list devices` output:
    [{"udid", "available", "paired", "line"}]. Simulators have a different UDID form and are skipped."""
    out = []
    for line in text.splitlines():
        m = _UDID.search(line)
        if not m or "iPhone" not in line or "simulated" in line:
            continue
        low = line.lower()
        out.append({"udid": m.group(1), "available": "unavailable" not in low,
                    "paired": "paired" in low and "unpaired" not in low and "not paired" not in low,
                    "line": line.strip()})
    return out


def developer_mode(text: str, udid: str) -> bool | None:
    """Developer Mode for `udid` from `devmodectl list` output; None if it isn't listed."""
    for line in text.splitlines():
        if udid in line:
            low = line.lower()
            if "disabled" in low:
                return False
            if "enabled" in low:
                return True
    return None


def device_udid() -> str:
    r = _run(["xcrun", "devicectl", "list", "devices"])
    for d in parse_devices(r.stdout):
        if d["available"]:
            return d["udid"]
    raise RuntimeError("no iPhone connected (plug it in and unlock it)")


def _record(entry: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    entry["ts"] = time.time()
    with open(HISTORY, "a") as f:
        f.write(json.dumps(entry) + "\n")

