"""Build, install and launch the pocketnode iOS app (device or simulator).

The app's sources ship inside the package (`pocketnode/ios`). Each build copies
them into a working directory under the pocketnode home (~/.pocketnode/ios),
generates the Xcode project there with XcodeGen and builds it, so nothing is
ever written next to the installed package.

Two flavours share the sources, chosen by the config key "flavour":
"dev" (the default) is the sideloaded app, with the Mac's own packages retagged
for iOS and a pairing token baked in. "store" is the App Store edition: its own
working directory (~/.pocketnode/ios-store), spec (project.store.yml), bundle
identifier and stage, a fixed package set from iOS wheels (wheels.py), no token,
and `archive()` for an App Store archive.
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
FLAVOURS = ("dev", "store")
STORE_WORK_DIR = CONFIG_DIR / "ios-store"
STORE_BUNDLE_ID = "dev.pocketnode.store"
STORE_SIMULATOR = ("iPhone 17 Pro", "64323BE2-0866-4214-85AF-EAA02A7B53C7")
STORE_PORT = 7161
SPEC = {"dev": "project.yml", "store": "project.store.yml"}


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


def current_flavour(cfg: dict | None = None) -> str:
    """The configured flavour: "dev" (sideloaded) or "store" (App Store edition)."""
    f = (config() if cfg is None else cfg).get("flavour", "dev")
    return f if f in FLAVOURS else "dev"


def work_dir(flavour: str = "dev") -> Path:
    return STORE_WORK_DIR if flavour == "store" else WORK_DIR


def stage_dir(sdk: str, flavour: str = "dev") -> Path:
    """Where `pocketnode app build` stages packages for `sdk` (install_python.sh reads the same place)."""
    root = Path(stage_mod.STAGE_ROOT)
    return root / "store" / sdk if flavour == "store" else root / sdk


def product(sdk: str, flavour: str = "dev", configuration: str = "Debug") -> Path:
    """Where a build for `sdk` puts the app."""
    kind = f"{configuration}-iphonesimulator" if sdk == "iphonesimulator" else f"{configuration}-iphoneos"
    return work_dir(flavour) / "build" / "Build" / "Products" / kind / "PocketNode.app"


def archive_path() -> Path:
    return STORE_WORK_DIR / "build" / "PocketNode.xcarchive"


def bundle_id(sdk: str = "iphoneos", flavour: str = "dev") -> str:
    if flavour == "store":
        return store_bundle_id()
    return configured_bundle_id(sdk, cfg=config())


def store_bundle_id(cfg: dict | None = None) -> str:
    return (config() if cfg is None else cfg).get("store_bundle_id") or STORE_BUNDLE_ID


def marketing_version() -> str:
    """pocketnode's version as App Store Connect accepts it (up to three integers)."""
    from packaging.version import InvalidVersion, Version
    try:
        rel = Version(md.version("pocketnode")).release
    except (md.PackageNotFoundError, InvalidVersion):
        rel = (0, 1, 0)
    return ".".join(str(x) for x in (*rel, 0, 0)[:3])


def prepare_project(team: str, bundle: str, flavour: str = "dev", env: dict | None = None) -> Path:
    """Copy the app sources into the working directory and generate the Xcode project there."""
    if flavour == "store":
        return _prepare(STORE_WORK_DIR, ("App", "scripts", "Store"), SPEC["store"],
                        {"POCKETNODE_TEAM": team, "POCKETNODE_BUNDLE_ID": bundle, **(env or {})})
    return _prepare(WORK_DIR, ("App", "scripts"), SPEC["dev"],
                    {"POCKETNODE_TEAM": team, "POCKETNODE_BUNDLE_ID": bundle})


def _prepare(work: Path, dirs: tuple[str, ...], spec: str, xenv: dict) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    for name in dirs:
        shutil.rmtree(work / name, ignore_errors=True)
        shutil.copytree(SOURCE_DIR / name, work / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(SOURCE_DIR / spec, work / spec)
    xcf, link = tools.python_xcframework(), work / "Python.xcframework"
    if link.is_symlink() or link.exists():
        if link.resolve() == xcf.resolve():
            xcf = None
        elif link.is_symlink():
            link.unlink()
        else:
            shutil.rmtree(link)
    if xcf is not None:
        link.symlink_to(xcf)
    cmd = [tools.xcodegen(), "generate", "-q"] + (["--spec", spec] if spec != SPEC["dev"] else [])
    r = _run(cmd, cwd=work, env={**os.environ, **xenv})
    if r.returncode:
        raise RuntimeError("xcodegen failed: " + (r.stderr or r.stdout).strip())
    return work


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
          bundle_id: str | None = None, restage: bool | str = True, flavour: str | None = None) -> Path:
    """Stage packages, generate the Xcode project and build. Returns the .app path.

    restage: True restages everything, "pocketnode" refreshes only pocketnode
    itself (fast while developing), False reuses the existing stage.
    flavour: "dev" or "store" (default: the configured one). The store
    flavour's packages come from its package list, not `pkgs`.
    """
    if (flavour or current_flavour()) == "store":
        return build_store(sdk, team=team, bundle=bundle_id, restage=restage)
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


# ------------------------------------------------------------ store flavour

def store_settings(configuration: str = "Debug", cfg: dict | None = None) -> dict:
    """The store build's pocketnode_settings.json (the build script sets "debug" from the configuration again)."""
    cfg = config() if cfg is None else cfg
    return {"edition": "store", "tls": bool(cfg.get("store_tls", True)),
            "privacy_url": cfg.get("store_privacy_url"), "debug": configuration == "Debug"}


def _store_env(cfg: dict) -> dict:
    return {"POCKETNODE_MARKETING_VERSION": marketing_version(),
            "POCKETNODE_BUILD": str(int(cfg.get("store_build", 1)))}


def stage_store(sdk: str, bundle: str, restage: bool | str = True) -> None:
    """Stage the store flavour's fixed package set for `sdk` from iOS wheels."""
    from . import wheels
    out = stage_dir(sdk, "store")
    if restage == "pocketnode" and (out / "app_packages").is_dir():
        wheels.refresh_pocketnode(out)
    elif restage or not (out / "app_packages").is_dir():
        cfg = config()
        reqs = wheels.read_requirements(wheels.packages_file(cfg))
        rep = wheels.stage_store(reqs, sdk, out, bundle, wheelhouse=wheels.wheelhouse(cfg))
        print(f"  staged {len(rep.dists)} distributions from iOS wheels ({rep.binaries} binaries)")


def build_store(sdk: str = "iphonesimulator", team: str | None = None, bundle: str | None = None,
                restage: bool | str = True, configuration: str = "Debug") -> Path:
    """Build the App Store edition for `sdk` (a development build). Returns the .app path."""
    cfg = config()
    team = team if team is not None else cfg.get("team", "")
    if not team and sdk == "iphoneos":
        team = detect_team()
        save_config(team=team)
    bundle = bundle or store_bundle_id(cfg)
    t0 = time.time()
    stage_store(sdk, bundle, restage)
    t1 = time.time()
    (CONFIG_DIR / "store-settings.json").write_text(json.dumps(store_settings(configuration, cfg)))
    work = prepare_project(team, bundle, "store", _store_env(cfg))
    env = {**os.environ, "POCKETNODE_TEAM": team, "POCKETNODE_BUNDLE_ID": bundle,
           "POCKETNODE_HOME": str(CONFIG_DIR), "POCKETNODE_STAGE": str(stage_mod.STAGE_ROOT)}
    dest = "generic/platform=iOS Simulator" if sdk == "iphonesimulator" else "generic/platform=iOS"
    cmd = ["xcodebuild", "-project", "PocketNode.xcodeproj", "-scheme", "PocketNode", "-sdk", sdk,
           "-destination", dest, "-derivedDataPath", "build", "-configuration", configuration, "build"]
    cmd += ["CODE_SIGN_IDENTITY=-"] if sdk == "iphonesimulator" else ["-allowProvisioningUpdates"]
    log = work / "build" / f"{sdk}.log"
    log.parent.mkdir(exist_ok=True)
    r = _run(cmd, log=log, cwd=work, env=env)
    if r.returncode:
        raise RuntimeError(build_failure(r.stdout + r.stderr, bundle, log))
    _record({"event": "build", "flavour": "store", "sdk": sdk, "stage_s": t1 - t0, "build_s": time.time() - t1})
    return product(sdk, "store", configuration)


def archive(team: str | None = None, restage: bool | str = True) -> tuple[Path, bool]:
    """`xcodebuild archive` of the App Store edition (Release, generic iOS device).

    Returns (archive path, signed). Without a team the archive is built with
    code signing off: its structure can be checked (`pocketnode app validate`),
    but exporting it for upload needs the owner's distribution signing.
    """
    cfg = config()
    team = team if team is not None else cfg.get("team", "")
    bundle = store_bundle_id(cfg)
    stage_store("iphoneos", bundle, restage)
    (CONFIG_DIR / "store-settings.json").write_text(json.dumps(store_settings("Release", cfg)))
    work = prepare_project(team, bundle, "store", _store_env(cfg))
    arc = archive_path()
    if arc.exists():
        shutil.rmtree(arc)
    env = {**os.environ, "POCKETNODE_TEAM": team, "POCKETNODE_BUNDLE_ID": bundle,
           "POCKETNODE_HOME": str(CONFIG_DIR), "POCKETNODE_STAGE": str(stage_mod.STAGE_ROOT)}
    cmd = ["xcodebuild", "archive", "-project", "PocketNode.xcodeproj", "-scheme", "PocketNode",
           "-configuration", "Release", "-destination", "generic/platform=iOS", "-archivePath", str(arc),
           "-derivedDataPath", "build"]
    cmd += ["-allowProvisioningUpdates"] if team else \
        ["CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO", "CODE_SIGN_IDENTITY="]
    log = work / "build" / "archive.log"
    log.parent.mkdir(exist_ok=True)
    r = _run(cmd, log=log, cwd=work, env=env)
    if r.returncode:
        raise RuntimeError(build_failure(r.stdout + r.stderr, bundle, log))
    _record({"event": "archive", "flavour": "store", "signed": bool(team)})
    return arc, bool(team)


def store_simulator(device: str | None = None) -> str:
    """UDID of the simulator for the store flavour: `device` (a name or UDID), else the iPhone 17 Pro."""
    r = _run(["xcrun", "simctl", "list", "devices", "available", "-j"])
    devs = [d for rt, ds in json.loads(r.stdout)["devices"].items() if "iOS" in rt for d in ds]
    wanted = [device] if device else [STORE_SIMULATOR[1], STORE_SIMULATOR[0]]
    for w in wanted:
        for d in devs:
            if w in (d["udid"], d["name"]):
                return d["udid"]
    raise RuntimeError(f"no simulator named {wanted[0]} (`xcrun simctl list devices available` lists them)")


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


def install_sim(app: Path, device: str | None = None, launch: bool = True, flavour: str = "dev",
                port: int | None = None) -> None:
    """Install on a simulator and launch. The store flavour defaults to its own simulator
    (iPhone 17 Pro) and port 7161; `port` reaches the app as POCKETNODE_PORT."""
    if flavour == "store":
        device = store_simulator(device)
        port = port or STORE_PORT
    device = device or simulator()
    _run(["xcrun", "simctl", "boot", device])
    _run(["xcrun", "simctl", "bootstatus", device, "-b"])
    bid = bundle_id("iphonesimulator", flavour)
    _run(["xcrun", "simctl", "terminate", device, bid])
    r = _run(["xcrun", "simctl", "install", device, str(app)])
    if r.returncode:
        raise RuntimeError(r.stderr)
    if launch:
        if port:
            _run(["xcrun", "simctl", "launch", device, bid],
                 env={**os.environ, "SIMCTL_CHILD_POCKETNODE_PORT": str(port)})
        else:
            _run(["xcrun", "simctl", "launch", device, bid])


def install_device(app: Path, udid: str | None = None, launch: bool = True, flavour: str = "dev") -> None:
    from ..sources import launch_failure
    udid = udid or device_udid()
    t0 = time.time()
    r = _run(["xcrun", "devicectl", "device", "install", "app", "--device", udid, str(app)])
    if r.returncode:
        raise RuntimeError(r.stderr or r.stdout)
    _record({"event": "install", "seconds": time.time() - t0})
    if launch:
        r = _run(["xcrun", "devicectl", "device", "process", "launch", "--device", udid, bundle_id("iphoneos", flavour)])
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

