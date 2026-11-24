"""`pocketnode setup`'s checks, against recorded command output (no Xcode or phone needed),
the per-team bundle identifier, and the app sources shipped as package data."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("POCKETNODE_HOME", tempfile.mkdtemp(prefix="pn-test-home-"))

from pocketnode import envinfo, sources  # noqa: E402
from pocketnode.provision import app as app_mod  # noqa: E402
from pocketnode.provision import setup  # noqa: E402
from pocketnode.provision import stage as stage_mod  # noqa: E402

UDID = "00001111-0022334455667788"  # made up, in the physical-device form
SIM_UDID = "11111111-2222-3333-4444-555555555555"


def cp(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def runner(table: dict[str, subprocess.CompletedProcess]):
    """A fake subprocess.run: the first key that starts the joined command decides the result."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        line = " ".join(cmd)
        for k, v in table.items():
            if line.startswith(k):
                return v
        raise AssertionError(f"unexpected command {line}")
    run.calls = calls
    return run


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(app_mod, "CONFIG_DIR", h)
    monkeypatch.setattr(app_mod, "HISTORY", h / "build-history.jsonl")
    monkeypatch.setattr(app_mod, "WORK_DIR", h / "ios")
    monkeypatch.setattr(sources, "CONFIG_DIR", h)
    monkeypatch.setattr(stage_mod, "STAGE_ROOT", h / "build")
    return h


# ------------------------------------------------------------ platform, Xcode

def test_platform():
    assert setup.check_platform("Darwin", "arm64", (3, 13, 7)).ok
    c = setup.check_platform("Darwin", "x86_64", (3, 13, 7))
    assert not c.ok and "Rosetta" in c.fix
    c = setup.check_platform("Darwin", "arm64", (3, 12, 4))
    assert not c.ok and "3.13" in c.fix and c.message == "Python 3.12.4"
    assert not setup.check_platform("Linux", "x86_64", (3, 13, 0)).ok


XCODE_OK = {
    "xcode-select -p": cp("/Applications/Xcode.app/Contents/Developer\n"),
    "xcodebuild -version": cp("Xcode 27.1\nBuild version 27A9275\n"),
    "xcodebuild -checkFirstLaunchStatus": cp(),
}


def test_xcode_ok():
    c = setup.check_xcode(runner(XCODE_OK))
    assert c.ok and c.message.startswith("Xcode 27.1")


def test_xcode_not_installed_or_only_command_line_tools():
    c = setup.check_xcode(runner({"xcode-select -p": cp(stderr="xcode-select: error: unable to get active "
                                                               "developer directory", code=2)}))
    assert not c.ok and "App Store" in c.fix
    c = setup.check_xcode(runner({"xcode-select -p": cp("/Library/Developer/CommandLineTools\n")}))
    assert not c.ok and "xcode-select -s /Applications/Xcode.app" in c.fix


def test_xcode_licence_first_launch_and_age():
    lic = {**XCODE_OK, "xcodebuild -version": cp(stderr="You have not agreed to the Xcode license agreements. "
                                                        "Please run 'sudo xcodebuild -license' ...", code=69)}
    c = setup.check_xcode(runner(lic))
    assert not c.ok and "sudo xcodebuild -license accept" in c.fix
    first = {**XCODE_OK, "xcodebuild -checkFirstLaunchStatus": cp(code=69)}
    c = setup.check_xcode(runner(first))
    assert not c.ok and "-runFirstLaunch" in c.fix
    old = {**XCODE_OK, "xcodebuild -version": cp("Xcode 16.4\nBuild version 16F6\n")}
    c = setup.check_xcode(runner(old))
    assert not c.ok and "too old" in c.message


def test_ios_sdk():
    assert setup.check_ios_sdk(runner({"xcrun": cp("27.1\n")})).message == "iOS 27.1 SDK"
    c = setup.check_ios_sdk(runner({"xcrun": cp(stderr="xcrun: error: SDK \"iphoneos\" cannot be located",
                                                code=1)}))
    assert not c.ok and "-downloadPlatform iOS" in c.fix


def test_metal():
    ok = runner({"xcrun -sdk iphoneos metal": cp("Apple metal version 32023.921 (metalfe-32023.921.6)\n"
                                                 "Target: air64-apple-darwin25.6.0\n")})
    assert setup.check_metal(ok).ok
    missing = runner({"xcrun -sdk iphoneos metal": cp(stderr="error: cannot execute tool 'metal' due to "
                                                             "missing Metal Toolchain; use: xcodebuild "
                                                             "-downloadComponent MetalToolchain", code=1)})
    c = setup.check_metal(missing)
    assert not c.ok and "-downloadComponent MetalToolchain" in c.fix and "MB" in c.fix


# --------------------------------------------------------------- signing

def test_signing_messages():
    c = setup.check_signing({}, signed_in=False)
    assert not c.ok and "Sign In" in c.fix
    c = setup.check_signing({}, signed_in=True)
    assert not c.ok and "click the account once" in c.fix
    c = setup.check_signing({"AAAAA11111": "A (Personal Team)", "BBBBB22222": "B Inc"}, signed_in=True)
    assert not c.ok and "--team" in c.fix
    c = setup.check_signing({"AAAAA11111": "A (Personal Team)"}, signed_in=True)
    assert c.ok and "AAAAA11111" in c.message and "7 days" in c.message
    c = setup.check_signing({"BBBBB22222": "B Inc"}, signed_in=True)
    assert c.ok and "7 days" not in c.message
    assert not setup.check_signing({"AAAAA11111": "A"}, True, team="CCCCC33333").ok
    assert setup.check_signing({"AAAAA11111": "A", "BBBBB22222": "B"}, True, team="BBBBB22222").ok


# ----------------------------------------------------------------- phone

DEVICES = f"""\
Name             Hostname   Identifier                                    State                Model                        Reality
--------------   --------   -------------------------------------------   ------------------   --------------------------   ---------
Someone's iPhone            {UDID} (UDID)              available (paired)   iPhone 18 Pro (iPhone19,2)   physical
iPhone 18 Pro               {SIM_UDID} (UDID)   connected            iPhone 18 Pro (iPhone19,2)   simulated
"""


def test_parse_devices_skips_simulators():
    devs = app_mod.parse_devices(DEVICES)
    assert [d["udid"] for d in devs] == [UDID]
    assert devs[0]["available"] and devs[0]["paired"]


def test_phone_states():
    c, udid = setup.check_phone(DEVICES)
    assert c.ok and udid == UDID
    c, udid = setup.check_phone(DEVICES.replace("available (paired)", "unavailable       "))
    assert not c.ok and udid is None and "cable" in c.fix
    c, udid = setup.check_phone(DEVICES.replace("available (paired)", "available         "))
    assert not c.ok and udid == UDID and "Trust" in c.fix
    c, _ = setup.check_phone("No devices found.\n")
    assert not c.ok


def test_developer_mode():
    on = f"UDID\t\t\t\tDeveloper Mode Status\n{UDID}\tenabled\n"
    assert setup.check_devmode(on, UDID).ok
    c = setup.check_devmode(on.replace("enabled", "disabled"), UDID)
    assert not c.ok and "Settings > Privacy & Security > Developer Mode" in c.fix
    assert app_mod.developer_mode(on, "00009999-000000000000AAAA") is None
    assert setup.check_devmode("UDID\tDeveloper Mode Status\n", UDID).ok  # not listed: the build decides


def test_lock_state():
    def doc(**result):
        return json.dumps({"info": {"outcome": "success"}, "result": result})
    assert setup.parse_lock_state(doc(passcodeRequired=True, unlockedSinceBoot=True)) is True
    assert setup.parse_lock_state(doc(isPasscodeRequired=False, hasBeenUnlockedSinceBoot=True)) is False
    assert setup.parse_lock_state(doc(unlockedSinceBoot=False)) is True
    assert setup.parse_lock_state("not json") is None
    assert setup.parse_lock_state(doc()) is None
    assert not setup.check_unlocked(doc(passcodeRequired=True)).ok
    assert setup.check_unlocked(doc(passcodeRequired=False)).ok
    assert setup.check_unlocked("").ok  # unknown: don't block; a launch on a locked phone says so


# --------------------------------------------------------------- launching

UNTRUSTED = ("ERROR: The application failed to launch. (com.apple.dt.CoreDeviceError error 10002.)\n"
             "Unable to launch dev.pocketnode.x because it has an invalid code signature, inadequate "
             "entitlements or its profile has not been explicitly trusted by the user.")


def test_launch_failure_kinds():
    assert sources.launch_failure(UNTRUSTED)[0] == "untrusted"
    assert sources.launch_failure("The device was not, or could not be, unlocked.")[0] == "locked"
    assert sources.launch_failure("The requested application is not installed.")[0] == "missing"
    assert sources.launch_failure("something else")[0] == "other"
    assert "VPN & Device Management" in sources.launch_failure(UNTRUSTED)[1]


def _launch_harness(launch_result, answers_after: int):
    launches, polls, said = [], [0], []

    def launch():
        launches.append(1)
        return launch_result

    def answers():
        polls[0] += 1
        return polls[0] > answers_after

    c = setup.first_launch(launch, answers, said.append, poll=0, sleep=lambda s: None)
    return c, launches, polls[0], said


def test_untrusted_launch_is_tried_once_then_waits():
    c, launches, polls, said = _launch_harness(cp(stderr=UNTRUSTED, code=1), answers_after=25)
    assert c.ok
    assert len(launches) == 1  # every failed launch puts an alert on the phone: never twice
    assert polls == 26
    text = "\n".join(said)
    assert "Settings > General > VPN & Device Management" in text and "Trust" in text
    assert "open the pocketnode app" in text


def test_launch_that_works_and_app_already_running():
    c, launches, _, said = _launch_harness(cp(), answers_after=3)
    assert c.ok and len(launches) == 1 and not said
    c, launches, _, _ = _launch_harness(cp(), answers_after=0)
    assert c.ok and launches == []  # already answering: nothing to launch


def test_launch_of_a_missing_app_stops():
    c, launches, _, _ = _launch_harness(cp(stderr="The requested application is not installed.", code=1),
                                        answers_after=10**9)
    assert not c.ok and len(launches) == 1


def test_step_waits_until_fixed(monkeypatch):
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)
    out = []
    s = setup.Setup(out=out.append, interval=0)
    states = iter([setup.Check(False, "Developer Mode is off", "turn it on"),
                   setup.Check(False, "Developer Mode is off", "turn it on"),
                   setup.Check(False, "the phone is locked", "unlock it"),
                   setup.Check(True, "on")])
    assert s.step("Developer Mode", lambda: next(states)).ok
    assert out[0] == "Developer Mode: Developer Mode is off"
    assert any("waiting" in l for l in out) and any("now: the phone is locked" in l for l in out)
    assert out[-1] == "  done: on"
    with pytest.raises(setup.Stop):
        setup.Setup(out=out.append).step("Mac and Python", lambda: setup.Check(False, "Python 3.12", "x"),
                                         wait=False)


# ---------------------------------------------------------- bundle identifier

def test_bundle_id_per_team(home):
    assert sources.bundle_id("iphoneos", cfg={"team": "AB12CD34EF"}) == "dev.pocketnode.ab12cd34ef"
    assert sources.bundle_id("iphonesimulator", cfg={"team": "AB12CD34EF"}) == "dev.pocketnode.app"
    assert sources.bundle_id("iphoneos", cfg={"team": "AB12CD34EF", "bundle_id": "org.me.pn"}) == "org.me.pn"
    assert sources.bundle_id("iphonesimulator", cfg={"bundle_id": "org.me.pn"}) == "org.me.pn"
    saved = {"team": "AB12CD34EF", "bundle_ids": {"AB12CD34EF": "dev.pocketnode.older"}}
    assert sources.bundle_id("iphoneos", cfg=saved) == "dev.pocketnode.older"
    app_mod.save_config(team="AB12CD34EF")
    assert app_mod.bundle_id("iphoneos") == sources.bundle_id("iphoneos") == "dev.pocketnode.ab12cd34ef"


def test_device_build_uses_and_saves_the_team_bundle_id(home, monkeypatch):
    seen = {}
    monkeypatch.setattr(stage_mod, "stage", lambda pkgs, sdk, bundle_id: seen.setdefault("stage", bundle_id)
                        and stage_mod.Report(sdk))
    monkeypatch.setattr(app_mod, "prepare_project", lambda team, bundle: seen.setdefault("project", bundle)
                        and home)
    monkeypatch.setattr(app_mod, "device_udid", lambda: (_ for _ in ()).throw(RuntimeError("no phone")))
    monkeypatch.setattr(app_mod, "profile_expiry", lambda p: None)

    def fake_run(cmd, log=None, **kw):
        seen["env"] = kw.get("env", {})
        return cp()
    monkeypatch.setattr(app_mod, "_run", fake_run)
    app_mod.save_config(team="ZZ99YY88XX", packages=[])
    app_mod.build("iphoneos")
    assert seen["stage"] == seen["project"] == seen["env"]["POCKETNODE_BUNDLE_ID"] == "dev.pocketnode.zz99yy88xx"
    assert app_mod.config()["bundle_ids"] == {"ZZ99YY88XX": "dev.pocketnode.zz99yy88xx"}
    assert json.loads((home / "app-settings.json").read_text())["torch_mps"] is True
    app_mod.build("iphonesimulator")
    assert seen["env"]["POCKETNODE_BUNDLE_ID"] == "dev.pocketnode.app"
    assert json.loads((home / "app-settings.json").read_text())["torch_mps"] is False
    # --bundle-id wins, for both
    app_mod.save_config(bundle_id="org.example.pn")
    app_mod.build("iphoneos")
    assert seen["env"]["POCKETNODE_BUNDLE_ID"] == "org.example.pn"


def test_hot_add_signs_with_the_app_bundle_id(home, monkeypatch):
    from pocketnode.provision import hot
    app_mod.save_config(team="ZZ99YY88XX")
    seen = {}

    class FakeStager:
        def __init__(self, sdk, out, bundle_id, inplace):
            seen["stager"] = bundle_id
            self.pk = Path(out) / "app_packages"
            self.pk.mkdir(parents=True)
            (self.pk / "lib.so").write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 28)

        def stage(self, names, only=None):
            return stage_mod.Report("iphoneos")

    monkeypatch.setattr(stage_mod, "Stager", FakeStager)
    monkeypatch.setattr(hot, "signing_identity", lambda: "Apple Development: Someone (TEAM)")
    monkeypatch.setattr(hot.subprocess, "run", lambda cmd, **kw: seen.setdefault("codesign", cmd) and cp())
    hot.prepare(["x"], {}, "iphoneos", home / "out")
    assert seen["stager"] == "dev.pocketnode.zz99yy88xx"
    i = seen["codesign"].index("--identifier")
    assert seen["codesign"][i + 1] == "dev.pocketnode.zz99yy88xx"


def test_build_failure_messages(tmp_path):
    taken = ('error: Failed Registering Bundle Identifier: The app identifier "dev.pocketnode.app" cannot be '
             'registered to your development team because it is not available.')
    msg = app_mod.build_failure(taken, "dev.pocketnode.app", tmp_path / "log")
    assert "--bundle-id" in msg and "dev.pocketnode.app" in msg
    msg = app_mod.build_failure("error: No Account for Team \"X\".", "b", tmp_path / "log")
    assert "Apple Accounts" in msg
    assert "full log" in app_mod.build_failure("error: something new", "b", tmp_path / "log")
    script = ("Signing framework...\nxargs: command line cannot be assembled, too long\n"
              "Command PhaseScriptExecution failed with a nonzero exit code\n** BUILD FAILED **\n")
    msg = app_mod.build_failure(script, "b", tmp_path / "log")
    assert "xargs: command line cannot be assembled" in msg and "PhaseScriptExecution" not in msg


# ----------------------------------------------------- app sources as package data

def test_app_sources_ship_in_the_package():
    src = app_mod.SOURCE_DIR
    assert src.parent.name == "pocketnode"
    for rel in ("project.yml", "App/Info.plist", "App/AppDelegate.m", "App/python/pocketnode_ios.py",
                "scripts/install_python.sh"):
        assert (src / rel).is_file(), rel


def test_phone_copy_and_fingerprint_leave_out_the_app_sources(tmp_path, monkeypatch):
    import pocketnode
    root = Path(pocketnode.__file__).parent
    shutil.copytree(root, tmp_path / "pocketnode", ignore=stage_mod._IGNORE)
    assert not (tmp_path / "pocketnode" / "ios").exists()
    assert (tmp_path / "pocketnode" / "worker.py").exists()
    # The phone's copy has no ios/, so the fingerprint must not depend on it.
    mac = envinfo.source_version()
    monkeypatch.setattr(envinfo, "__file__", str(tmp_path / "pocketnode" / "envinfo.py"))
    assert envinfo.source_version() == mac


def test_project_generates_in_the_work_dir(home, monkeypatch):
    if shutil.which("xcodegen") is None:
        pytest.skip("xcodegen not on PATH")
    xcf = home / "cache" / "Python.xcframework"
    xcf.mkdir(parents=True)
    (xcf / "Info.plist").write_text("<plist/>")
    monkeypatch.setattr(app_mod.tools, "python_xcframework", lambda: xcf)
    work = app_mod.prepare_project("", "dev.pocketnode.app")
    assert work == home / "ios"
    assert (work / "PocketNode.xcodeproj" / "project.pbxproj").exists()
    assert (work / "Python.xcframework").resolve() == xcf.resolve()
    pbx = (work / "PocketNode.xcodeproj" / "project.pbxproj").read_text()
    assert "PRODUCT_BUNDLE_IDENTIFIER = dev.pocketnode.app" in pbx
    # Again: idempotent, and the link is kept.
    app_mod.prepare_project("", "dev.pocketnode.app")
    assert (work / "Python.xcframework").is_symlink()
