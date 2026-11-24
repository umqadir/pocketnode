"""The App Store edition's build system, without Xcode builds or a simulator:
the flavour switch, staging from iOS wheels (wheels.py) and the bundle checks
(validate.py) against small synthetic bundles. Binaries for these tests are
compiled with clang when the SDKs are there, else those tests are skipped."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("POCKETNODE_HOME", tempfile.mkdtemp(prefix="pn-test-home-"))

from pocketnode import sources  # noqa: E402
from pocketnode.provision import app as app_mod  # noqa: E402
from pocketnode.provision import stage as stage_mod  # noqa: E402
from pocketnode.provision import validate, wheels  # noqa: E402


def cp(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(app_mod, "CONFIG_DIR", h)
    monkeypatch.setattr(app_mod, "HISTORY", h / "build-history.jsonl")
    monkeypatch.setattr(app_mod, "WORK_DIR", h / "ios")
    monkeypatch.setattr(app_mod, "STORE_WORK_DIR", h / "ios-store")
    monkeypatch.setattr(sources, "CONFIG_DIR", h)
    monkeypatch.setattr(wheels, "CONFIG_DIR", h)
    monkeypatch.setattr(stage_mod, "STAGE_ROOT", h / "build")
    return h


# ------------------------------------------------------------ flavour switch

def test_flavour_paths_and_ids(home):
    assert app_mod.current_flavour() == "dev"
    assert app_mod.work_dir("dev") == home / "ios" and app_mod.work_dir("store") == home / "ios-store"
    assert app_mod.stage_dir("iphoneos") == home / "build" / "iphoneos"
    assert app_mod.stage_dir("iphoneos", "store") == home / "build" / "store" / "iphoneos"
    assert app_mod.product("iphonesimulator") == \
        home / "ios/build/Build/Products/Debug-iphonesimulator/PocketNode.app"
    assert app_mod.product("iphoneos", "store", "Release") == \
        home / "ios-store/build/Build/Products/Release-iphoneos/PocketNode.app"
    assert app_mod.archive_path() == home / "ios-store/build/PocketNode.xcarchive"
    assert app_mod.bundle_id("iphonesimulator") == "dev.pocketnode.app"
    assert app_mod.bundle_id("iphoneos", "store") == "dev.pocketnode.store"
    app_mod.save_config(flavour="store", store_bundle_id="org.example.pnstore", team="AB12CD34EF")
    assert app_mod.current_flavour() == "store"
    assert app_mod.bundle_id("iphoneos", "store") == "org.example.pnstore"
    assert app_mod.bundle_id("iphoneos") == "dev.pocketnode.ab12cd34ef"  # dev's own rules are untouched
    app_mod.save_config(flavour="nonsense")
    assert app_mod.current_flavour() == "dev"
    assert app_mod.SPEC == {"dev": "project.yml", "store": "project.store.yml"}


def test_marketing_version_is_three_integers():
    v = app_mod.marketing_version()
    assert len(v.split(".")) == 3 and all(x.isdigit() for x in v.split("."))


def _fake_build(monkeypatch, home):
    seen = {"prepare": [], "run": []}
    monkeypatch.setattr(app_mod, "stage_store", lambda sdk, bundle, restage: seen.setdefault("store_stage", (sdk, bundle)))
    monkeypatch.setattr(stage_mod, "stage", lambda pkgs, sdk, bundle_id: seen.setdefault("dev_stage", bundle_id)
                        and stage_mod.Report(sdk))

    def prepare(team, bundle, flavour="dev", env=None):
        seen["prepare"].append((team, bundle, flavour, env))
        app_mod.work_dir(flavour).mkdir(parents=True, exist_ok=True)
        return app_mod.work_dir(flavour)
    monkeypatch.setattr(app_mod, "prepare_project", prepare)

    def run(cmd, log=None, **kw):
        seen["run"].append((cmd, kw.get("env", {})))
        return cp()
    monkeypatch.setattr(app_mod, "_run", run)
    return seen


def test_store_build_has_its_own_everything_and_no_token(home, monkeypatch):
    seen = _fake_build(monkeypatch, home)
    out = app_mod.build("iphonesimulator", flavour="store")
    assert out == home / "ios-store/build/Build/Products/Debug-iphonesimulator/PocketNode.app"
    assert seen["store_stage"] == ("iphonesimulator", "dev.pocketnode.store")
    team, bundle, flavour, env = seen["prepare"][-1]
    assert (bundle, flavour) == ("dev.pocketnode.store", "store")
    assert env["POCKETNODE_BUILD"] == "1" and env["POCKETNODE_MARKETING_VERSION"] == app_mod.marketing_version()
    cmd, env = seen["run"][-1]
    assert env["POCKETNODE_BUNDLE_ID"] == "dev.pocketnode.store"
    assert "-configuration" in cmd and cmd[cmd.index("-configuration") + 1] == "Debug"
    assert not (home / "token").exists()
    assert not (home / "app-settings.json").exists()
    s = json.loads((home / "store-settings.json").read_text())
    assert s == {"edition": "store", "tls": True, "privacy_url": None, "debug": True}
    # store_build, store_tls and store_privacy_url reach the build
    app_mod.save_config(flavour="store", store_build=7, store_tls=False, store_privacy_url="https://x.example/p")
    app_mod.build("iphonesimulator", flavour="store")
    assert seen["prepare"][-1][3]["POCKETNODE_BUILD"] == "7"
    s = json.loads((home / "store-settings.json").read_text())
    assert s["tls"] is False and s["privacy_url"] == "https://x.example/p"
    assert not (home / "token").exists()


def test_dev_build_unchanged_next_to_store(home, monkeypatch):
    seen = _fake_build(monkeypatch, home)
    app_mod.save_config(packages=[])
    app_mod.build("iphonesimulator")  # no flavour configured: dev
    assert seen["prepare"][-1][2] == "dev" and seen["prepare"][-1][3] is None
    assert (home / "token").exists() and (home / "app-settings.json").exists()
    assert not (home / "store-settings.json").exists()
    assert seen["run"][-1][1]["POCKETNODE_BUNDLE_ID"] == "dev.pocketnode.app"
    app_mod.save_config(flavour="store")
    app_mod.build("iphonesimulator")  # a saved "store" doesn't reach dev-only callers
    assert seen["prepare"][-1][2] == "dev"
    assert not (home / "store-settings.json").exists()


def test_setup_builds_the_dev_flavour_whatever_is_saved(home, monkeypatch):
    from pocketnode.provision import setup
    app_mod.save_config(flavour="store")
    got = []
    monkeypatch.setattr(app_mod, "build", lambda sdk, **kw: got.append(kw) or home / "x.app")
    monkeypatch.setattr(app_mod, "install_sim", lambda p, **kw: got.append(("installed", kw)))
    setup.Setup(out=lambda s: None).build_install("iphonesimulator", None)
    assert got[0] == {"flavour": "dev"} and got[1] == ("installed", {})


def test_archive_unsigned_without_team_signed_with(home, monkeypatch):
    seen = _fake_build(monkeypatch, home)
    app_mod.save_config(team="AB12CD34EF")  # saved by a dev build: never signs an archive
    arc, signed = app_mod.archive()
    cmd = seen["run"][-1][0]
    assert not signed and arc == home / "ios-store/build/PocketNode.xcarchive"
    assert cmd[:2] == ["xcodebuild", "archive"] and "CODE_SIGNING_ALLOWED=NO" in cmd
    assert cmd[cmd.index("-configuration") + 1] == "Release"
    assert cmd[cmd.index("-destination") + 1] == "generic/platform=iOS"
    assert cmd[cmd.index("-archivePath") + 1] == str(arc)
    assert seen["store_stage"][0] == "iphoneos"
    assert json.loads((home / "store-settings.json").read_text())["debug"] is False
    arc, signed = app_mod.archive(team="ZZ99YY88XX")
    cmd = seen["run"][-1][0]
    assert signed and "CODE_SIGNING_ALLOWED=NO" not in cmd and "-allowProvisioningUpdates" in cmd
    assert seen["prepare"][-1][0] == "ZZ99YY88XX"
    assert not (home / "token").exists()


SIMS = json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
    {"name": "iPhone 18 Pro", "udid": "F9730BBF-0000-0000-0000-000000000000", "state": "Booted"},
    {"name": "iPhone 17 Pro", "udid": app_mod.STORE_SIMULATOR[1], "state": "Shutdown"},
    {"name": "iPhone Air", "udid": "AAAAAAAA-0000-0000-0000-000000000000", "state": "Shutdown"},
]}})


def test_store_install_uses_its_simulator_and_port(home, monkeypatch):
    calls = []

    def run(cmd, log=None, **kw):
        calls.append((cmd, kw.get("env") or {}))
        return cp(SIMS) if cmd[:3] == ["xcrun", "simctl", "list"] else cp()
    monkeypatch.setattr(app_mod, "_run", run)
    app_mod.install_sim(home / "PocketNode.app", flavour="store")
    launch = [c for c in calls if c[0][2] == "launch"][-1]
    assert launch[0][3:] == [app_mod.STORE_SIMULATOR[1], "dev.pocketnode.store"]
    assert launch[1]["SIMCTL_CHILD_POCKETNODE_PORT"] == "7161"
    calls.clear()
    app_mod.install_sim(home / "PocketNode.app", device="iPhone Air", flavour="store", port=7999)
    launch = [c for c in calls if c[0][2] == "launch"][-1]
    assert launch[0][3] == "AAAAAAAA-0000-0000-0000-000000000000"
    assert launch[1]["SIMCTL_CHILD_POCKETNODE_PORT"] == "7999"
    # dev keeps picking the 18 Pro, with no port override
    calls.clear()
    app_mod.install_sim(home / "PocketNode.app")
    launch = [c for c in calls if c[0][2] == "launch"][-1]
    assert launch[0][3:] == ["F9730BBF-0000-0000-0000-000000000000", "dev.pocketnode.app"] and not launch[1]


def test_cli_store_switch_and_back(home, monkeypatch, capsys):
    from pocketnode import cli
    got = []
    monkeypatch.setattr(app_mod, "build", lambda sdk, restage=True, flavour=None: got.append((sdk, flavour))
                        or home / "x.app")
    cli.main(["app", "build", "--store", "--sim", "--bundle-id", "org.example.s"])
    assert got[-1] == ("iphonesimulator", "store")
    c = app_mod.config()
    assert c["flavour"] == "store" and c["store_bundle_id"] == "org.example.s" and "bundle_id" not in c
    capsys.readouterr()
    cli.main(["app", "build", "--sim"])  # stays store, and says so
    assert got[-1] == ("iphonesimulator", "store")
    assert capsys.readouterr().out.startswith("building the App Store edition")
    cli.main(["app", "build", "--dev", "--sim"])
    assert app_mod.config()["flavour"] == "dev" and got[-1] == ("iphonesimulator", None)
    assert "App Store" not in capsys.readouterr().out


def test_cli_archive_signs_only_with_team_on_the_command_line(home, monkeypatch, capsys):
    from pocketnode import cli
    got = []
    monkeypatch.setattr(app_mod, "archive", lambda team=None, restage=True: got.append(team)
                        or (home / "a.xcarchive", bool(team)))
    app_mod.save_config(team="AB12CD34EF")
    cli.main(["app", "archive"])
    assert got[-1] is None and "without code signing" in capsys.readouterr().out
    cli.main(["app", "archive", "--team", "ZZ99YY88XX"])
    assert got[-1] == "ZZ99YY88XX" and "without code signing" not in capsys.readouterr().out
    assert app_mod.config()["team"] == "AB12CD34EF"  # an archive's --team isn't saved


def test_store_project_generates(home, monkeypatch):
    if shutil.which("xcodegen") is None:
        pytest.skip("xcodegen not on PATH")
    xcf = home / "cache" / "Python.xcframework"
    xcf.mkdir(parents=True)
    (xcf / "Info.plist").write_text("<plist/>")
    monkeypatch.setattr(app_mod.tools, "python_xcframework", lambda: xcf)
    work = app_mod.prepare_project("", "dev.pocketnode.store", "store",
                                   {"POCKETNODE_MARKETING_VERSION": "0.1.0", "POCKETNODE_BUILD": "3"})
    assert work == home / "ios-store" and not (home / "ios").exists()
    pbx = (work / "PocketNode.xcodeproj" / "project.pbxproj").read_text()
    assert "PRODUCT_BUNDLE_IDENTIFIER = dev.pocketnode.store" in pbx
    assert "POCKETNODE_STORE=1" in pbx and "CURRENT_PROJECT_VERSION = 3" in pbx
    assert "MARKETING_VERSION = 0.1.0" in pbx and "INFOPLIST_FILE = Store/Info.plist" in pbx
    assert "PrivacyInfo.xcprivacy in Resources" in pbx and "Assets.xcassets in Resources" in pbx
    assert "pn_core.c in Sources" not in pbx and "packages.txt" not in pbx
    assert "POCKETNODE_FLAVOUR=store" in pbx


def test_store_sources_ship_in_the_package():
    src = app_mod.SOURCE_DIR
    for rel in ("project.store.yml", "Store/Info.plist", "Store/PocketNode.entitlements",
                "Store/PrivacyInfo.xcprivacy", "Store/packages.txt",
                "Store/Assets.xcassets/AppIcon.appiconset/AppIcon.png",
                "Store/Assets.xcassets/LaunchBackground.colorset/Contents.json"):
        assert (src / rel).is_file(), rel
    info = plistlib.loads((src / "Store/Info.plist").read_bytes())
    assert "UIFileSharingEnabled" not in info and "LSSupportsOpeningDocumentsInPlace" not in info
    assert info["CFBundleShortVersionString"] == "$(MARKETING_VERSION)"
    assert info["UILaunchScreen"]["UIColorName"] == "LaunchBackground"
    ent = plistlib.loads((src / "Store/PocketNode.entitlements").read_bytes())
    assert ent == {"com.apple.developer.kernel.increased-memory-limit": True}
    cats = validate.manifest_categories(src / "Store/PrivacyInfo.xcprivacy")
    assert cats == {c: {r} for c, r in validate.CATEGORIES.items()}


# ------------------------------------------------------------------ wheels

def test_package_list():
    reqs = wheels.read_requirements(wheels.PACKAGES_FILE)
    names = [wheels._req_name(r) for r in reqs]
    assert names == ["numpy", "pillow", "kiwisolver", "markupsafe", "contourpy", "cffi", "pandas"]
    assert all("==" in r for r in reqs) and "numpy==2.5.2.post1" in reqs and "pandas==3.1.1" in reqs
    assert "pocketnode app wheel pandas 3.1.1" in wheels.PACKAGES_FILE.read_text()


def test_package_settings(home):
    assert wheels.wheelhouse({}) == home / "wheelhouse"
    assert wheels.wheelhouse({"store_wheelhouse": "/w"}) == Path("/w")
    assert wheels.packages_file({"store_packages_file": "/p.txt"}) == Path("/p.txt")


def test_missing_wheel_message(tmp_path):
    out = ("error: No solution found\n  Because pandas>=1.2.0,<=1.5.3 has no wheels with a matching Python ABI "
           "tag ...\nhint: Wheels are required for `pandas` because building from source is disabled")
    msg = wheels._no_wheel(out, ["numpy==2.5.2.post1", "pandas"], "iphoneos", tmp_path / "wh")
    assert msg.startswith("no cp313 iOS wheel") and "pandas" in msg.split("uv said")[0]
    assert "numpy" not in msg.split("uv said")[0] and str(tmp_path / "wh") in msg
    assert "pocketnode app wheel NAME VERSION" in msg  # unpinned: the generic form
    msg = wheels._no_wheel(out, ["numpy==2.5.2.post1", "pandas==3.1.1"], "iphonesimulator", tmp_path / "wh")
    assert "Build one with `pocketnode app wheel pandas 3.1.1`" in msg


def test_build_wheel_runs_the_recipe_into_the_wheelhouse(tmp_path):
    house = tmp_path / "wh"
    calls = []

    def stub(cmd, **kw):  # stands in for the minutes-long recipe
        calls.append(cmd)
        for tag in ("iphoneos", "iphonesimulator"):
            (Path(cmd[4]) / f"pandas-3.1.1-cp313-cp313-ios_13_0_arm64_{tag}.whl").write_bytes(b"w")
        return cp()
    built = wheels.build_wheel("pandas", "3.1.1", house=house, run=stub)
    assert calls == [["/bin/bash", str(wheels.RECIPE_DIR / "build_ios_wheel.sh"), "pandas", "3.1.1", str(house)]]
    assert [p.name for p in built] == ["pandas-3.1.1-cp313-cp313-ios_13_0_arm64_iphoneos.whl",
                                       "pandas-3.1.1-cp313-cp313-ios_13_0_arm64_iphonesimulator.whl"]
    with pytest.raises(wheels.WheelError, match="failed"):
        wheels.build_wheel("pandas", "3.1.1", house=house, run=lambda cmd, **kw: cp(code=1))
    with pytest.raises(wheels.WheelError, match="iphoneos"):
        wheels.build_wheel("lxml", "6.0.0", house=house, run=lambda cmd, **kw: (
            Path(cmd[4], "lxml-6.0.0-cp313-cp313-ios_13_0_arm64_iphonesimulator.whl").write_bytes(b"w"), cp())[1])
    with pytest.raises(wheels.WheelError, match="not a package"):
        wheels.build_wheel("pandas; rm", "3.1.1", house=house, run=stub)


def test_recipe_ships_and_checks_its_arguments():
    names = {p.name for p in wheels.RECIPE_DIR.iterdir()}
    assert {"build_ios_wheel.sh", "cibw_local.py", "fetch_sdist.py", "patch_pandas.py",
            "inspect_ios_wheel.sh", "audit_ios_wheel.sh"} <= names
    for p in wheels.RECIPE_DIR.iterdir():
        assert "/private/tmp" not in p.read_text() and "/Users/" not in p.read_text(), p.name
    r = subprocess.run(["/bin/bash", str(wheels.RECIPE_DIR / "build_ios_wheel.sh"), "pandas"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "usage:" in r.stderr
    spec = (app_mod.SOURCE_DIR / "project.store.yml").read_text()
    assert '"wheels/**"' in spec  # Mac-side tooling stays out of the app


def test_cli_wheel(home, monkeypatch, capsys):
    from pocketnode import cli
    got = []
    monkeypatch.setattr(wheels, "build_wheel", lambda n, v, house=None: got.append((n, v, house))
                        or [house / f"{n}-{v}-x.whl"])
    app_mod.save_config(store_wheelhouse=str(home / "wh"))
    cli.main(["app", "wheel", "pandas", "3.1.1"])
    out = capsys.readouterr().out
    assert got == [("pandas", "3.1.1", home / "wh")]
    assert str(home / "wh" / "pandas-3.1.1-x.whl") in out and "pandas==3.1.1" in out
    with pytest.raises(SystemExit, match="usage"):
        cli.main(["app", "wheel", "pandas"])


def test_test_only_binaries_left_out_unless_imported(tmp_path):
    pk = tmp_path / "pk"
    files = {
        "pkg/__init__.py": "",
        "pkg/_core/_used_tests.cpython-313-iphoneos.so": "x",
        "pkg/_core/_unused_tests.cpython-313-iphoneos.so": "x",
        "pkg/_core/_real.cpython-313-iphoneos.so": "x",
        "pkg/testing/print_tables.py": "from pkg._core._used_tests import f\n",
        "pkg/conftest.py": "from pkg._core._unused_tests import g\n",  # pytest only: doesn't count
        "pkg/tests/__init__.py": "",
        "pkg/tests/test_x.py": "from pkg._core import _unused_tests\n",
        "pkg/tests/_helper.cpython-313-iphoneos.so": "x",
        "pkg/testing/tests/_t.cpython-313-iphoneos.so": "x",
    }
    for rel, text in files.items():
        (pk / rel).parent.mkdir(parents=True, exist_ok=True)
        (pk / rel).write_text(text)
    keep, drop = wheels.test_binaries(pk)
    assert keep == {"pkg._core._used_tests": "pkg/testing/print_tables.py"}
    assert sorted(drop) == ["pkg._core._unused_tests", "pkg.testing.tests._t", "pkg.tests._helper"]
    wheels._prune(pk)
    left = sorted(str(p.relative_to(pk)) for p in pk.rglob("*") if p.is_file())
    assert left == ["pkg/__init__.py", "pkg/_core/_real.cpython-313-iphoneos.so",
                    "pkg/_core/_used_tests.cpython-313-iphoneos.so", "pkg/conftest.py",
                    "pkg/testing/print_tables.py"]


def test_numpy_keeps_only_the_test_module_numpy_testing_imports(tmp_path):
    # The Mac's numpy has the iOS wheel's layout: its Python files, and empty stand-ins for its .so files.
    np_dir = Path(__import__("numpy").__file__).parent
    for f in np_dir.rglob("*"):
        if f.suffix in (".py", ".so") and f.is_file():
            dst = tmp_path / "numpy" / f.relative_to(np_dir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(f.read_bytes() if f.suffix == ".py" else b"")
    keep, drop = wheels.test_binaries(tmp_path)
    if not keep and not drop:
        pytest.skip("this numpy has no test extension modules")
    assert set(keep) == {"numpy._core._multiarray_tests"}  # numpy/testing/print_coercion_tables.py imports it
    assert {"numpy._core._umath_tests", "numpy._core._rational_tests", "numpy._core._struct_ufunc_tests",
            "numpy._core._operand_flag_tests"} <= set(drop)


def test_pocketnode_copy_leaves_out_mac_only_parts(tmp_path):
    vers = wheels.copy_pocketnode(tmp_path)
    assert set(vers) == {"pocketnode", "cloudpickle"}
    assert (tmp_path / "pocketnode" / "worker.py").exists() and (tmp_path / "cloudpickle").is_dir()
    for gone in ("ios", "provision"):
        assert not (tmp_path / "pocketnode" / gone).exists()
    assert not list(tmp_path.rglob("__pycache__")) and not list(tmp_path.rglob("*.pyc"))


def _sdk(name: str) -> bool:
    return subprocess.run(["xcrun", "--sdk", name, "--show-sdk-path"], capture_output=True).returncode == 0


def cc(tmp: Path, out: Path, target: str, src: str = "int f(void){return 1;}\n", *flags: str) -> Path:
    sdk = "macosx" if "macos" in target else "iphonesimulator" if target.endswith("simulator") else "iphoneos"
    if shutil.which("xcrun") is None or not _sdk(sdk):
        pytest.skip(f"no {sdk} SDK")
    c = tmp / f"{out.name}.c"
    c.write_text(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    kind = [] if "-bundle" in flags or "-exe" in flags else ["-dynamiclib"]
    flags = tuple(f for f in flags if f != "-exe")
    subprocess.run(["xcrun", "-sdk", sdk, "clang", "-target", target, *kind, *flags, str(c), "-o", str(out)],
                   check=True, capture_output=True)
    return out


SIM = "arm64-apple-ios17.0-simulator"
DEVICE = "arm64-apple-ios17.0"


def test_wraps_extension_modules_as_frameworks(tmp_path):
    out = tmp_path / "stage"
    so = cc(tmp_path, out / "app_packages/pkg/sub/_ext.cpython-313-iphonesimulator.so", SIM)
    s = wheels.StoreStager("iphonesimulator", out, "dev.pocketnode.store")
    s.fw.mkdir(parents=True)
    assert s.wrap_all() == 1
    fw = out / "Frameworks/pkg.sub._ext.framework"
    assert (fw / "pkg.sub._ext").is_file() and not so.exists()
    info = plistlib.loads((fw / "Info.plist").read_bytes())
    assert info["CFBundlePackageType"] == "FMWK" and info["CFBundleExecutable"] == "pkg.sub._ext"
    assert info["CFBundleIdentifier"] == "dev.pocketnode.store.pkg.sub.-ext"
    assert info["CFBundleSupportedPlatforms"] == ["iPhoneSimulator"]
    fwork = out / "app_packages/pkg/sub/_ext.cpython-313-iphonesimulator.fwork"
    assert fwork.read_text() == "Frameworks/pkg.sub._ext.framework/pkg.sub._ext"
    assert (fw / "pkg.sub._ext.origin").read_text() == "app_packages/pkg/sub/_ext.cpython-313-iphonesimulator.fwork"


def test_refuses_a_macos_binary(tmp_path):
    out = tmp_path / "stage"
    cc(tmp_path, out / "app_packages/pkg/_mac.cpython-313-iphonesimulator.so", "arm64-apple-macos14.0")
    s = wheels.StoreStager("iphonesimulator", out, "dev.pocketnode.store")
    s.fw.mkdir(parents=True)
    with pytest.raises(wheels.WheelError) as e:
        s.wrap_all()
    assert "pkg/_mac.cpython-313-iphonesimulator.so" in str(e.value) and "macOS" in str(e.value)
    assert not list(s.fw.iterdir())


def test_device_binaries_need_encryption_info(tmp_path):
    good = cc(tmp_path, tmp_path / "good.dylib", DEVICE)
    bad = cc(tmp_path, tmp_path / "bad.dylib", DEVICE, "int f(void){return 1;}\n", "-Wl,-no_encryption")
    sim = cc(tmp_path, tmp_path / "sim.dylib", SIM)
    assert validate.binary_problem(good, "iphoneos") is None
    assert "LC_ENCRYPTION_INFO_64" in validate.binary_problem(bad, "iphoneos")
    assert "iOS Simulator" in validate.binary_problem(sim, "iphoneos")
    assert validate.binary_problem(sim, "iphonesimulator") is None


def test_bundled_dylibs_become_frameworks_with_fixed_references(tmp_path):
    out = tmp_path / "stage"
    lib = cc(tmp_path, out / "app_packages/pkg/.dylibs/libfoo.dylib", SIM, "int foo(void){return 7;}\n",
             "-install_name", "@loader_path/.dylibs/libfoo.dylib")
    cc(tmp_path, out / "app_packages/pkg/_ext.cpython-313-iphonesimulator.so", SIM,
       "int foo(void); int f(void){return foo();}\n", str(lib))
    s = wheels.StoreStager("iphonesimulator", out, "dev.pocketnode.store")
    s.fw.mkdir(parents=True)
    assert s.wrap_all() == 2
    ext = out / "Frameworks/pkg._ext.framework/pkg._ext"
    dep = out / "Frameworks/pkg.dylibs.libfoo.framework/pkg.dylibs.libfoo"
    assert ext.is_file() and dep.is_file()
    assert ("@rpath/pkg.dylibs.libfoo.framework/pkg.dylibs.libfoo", "load") in validate.macho(ext)[0].deps
    assert validate.macho(dep)[0].ident == "@rpath/pkg.dylibs.libfoo.framework/pkg.dylibs.libfoo"
    assert not (out / "Frameworks/pkg.dylibs.libfoo.framework/pkg.dylibs.libfoo.origin").exists()


# ---------------------------------------------------------------- validate

STAT = "#include <sys/stat.h>\nint f(const char *p){struct stat s; return stat(p, &s);}\n"


def synthetic_app(tmp: Path, framework_flags: tuple[str, ...] = ()) -> Path:
    """A minimal store-edition device app: executable, one extension-module framework, manifests."""
    app = tmp / "PocketNode.app"
    cc(tmp, app / "PocketNode", "arm64-apple-ios26.0", "int main(void){return 0;}\n", "-exe")
    fw = app / "Frameworks/m._ext.framework"
    cc(tmp, fw / "m._ext", DEVICE, STAT, *framework_flags)
    (fw / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundlePackageType": "FMWK", "CFBundleExecutable": "m._ext", "CFBundleIdentifier": "dev.pocketnode.store.m.-ext",
        "MinimumOSVersion": "17.0", "CFBundleSupportedPlatforms": ["iPhoneOS"]}))
    (app / "app_packages/m").mkdir(parents=True)
    (app / "app_packages/m/_ext.cpython-313-iphoneos.fwork").write_text("Frameworks/m._ext.framework/m._ext")
    (fw / "m._ext.origin").write_text("app_packages/m/_ext.cpython-313-iphoneos.fwork")
    shutil.copy(app_mod.SOURCE_DIR / "Store/PrivacyInfo.xcprivacy", app / "PrivacyInfo.xcprivacy")
    shutil.copy(app_mod.SOURCE_DIR / "Store/PrivacyInfo.xcprivacy", fw / "PrivacyInfo.xcprivacy")
    (app / "Assets.car").write_bytes(b"car")
    (app / "pocketnode_settings.json").write_text(json.dumps({"edition": "store", "tls": False, "debug": False}))
    (app / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": "PocketNode", "CFBundleIdentifier": "dev.pocketnode.store",
        "CFBundleShortVersionString": "0.1.0", "CFBundleVersion": "1", "MinimumOSVersion": "26.0",
        "DTSDKName": "iphoneos27.1", "DTPlatformVersion": "27.1", "DTPlatformName": "iphoneos",
        "CFBundleSupportedPlatforms": ["iPhoneOS"], "UIRequiredDeviceCapabilities": ["arm64"],
        "CFBundleIconName": "AppIcon", "UILaunchScreen": {}, "ITSAppUsesNonExemptEncryption": False,
        "NSLocalNetworkUsageDescription": "x", "BGTaskSchedulerPermittedIdentifiers": ["a.b.*"]}))
    return app


def results(path: Path) -> dict[str, validate.Result]:
    _, rs = validate.validate(path)
    return {r.name: r for r in rs if not r.info}


def failed(rs: dict[str, validate.Result]) -> set[str]:
    return {n for n, r in rs.items() if r.ok is False}


def test_validate_passes_a_clean_synthetic_app(tmp_path):
    rs = results(synthetic_app(tmp_path))
    assert failed(rs) == set(), {n: rs[n].detail for n in failed(rs)}
    assert rs["binaries: LC_ENCRYPTION_INFO_64 present"].ok is True
    assert "1 frameworks need one" in rs["privacy: a manifest in every framework that uses a required-reason API"].detail


def test_validate_finds_a_stray_so(tmp_path):
    app = synthetic_app(tmp_path)
    cc(tmp_path, app / "app_packages/m/_other.cpython-313-iphoneos.so", DEVICE)
    f = failed(results(app))
    assert {"bundle: Mach-O files only as the executable and one per framework",
            "bundle: no .so, .dylib, .a or .o files"} <= f


def test_validate_finds_missing_encryption_info(tmp_path):
    f = failed(results(synthetic_app(tmp_path, ("-Wl,-no_encryption",))))
    assert f == {"binaries: LC_ENCRYPTION_INFO_64 present"}


def test_validate_finds_a_leftover_token_and_stamp(tmp_path):
    app = synthetic_app(tmp_path)
    (app / "pocketnode_token").write_text("secret")
    (app / "Frameworks/m._ext.framework/.pn-stamp").write_text("x")
    rs = results(app)
    assert failed(rs) == {"bundle: no token, stamps, dotfiles, .DS_Store or __pycache__"}
    assert "pocketnode_token" in rs["bundle: no token, stamps, dotfiles, .DS_Store or __pycache__"].detail


def test_validate_finds_a_missing_privacy_manifest(tmp_path):
    app = synthetic_app(tmp_path)
    (app / "Frameworks/m._ext.framework/PrivacyInfo.xcprivacy").unlink()
    rs = results(app)
    name = "privacy: a manifest in every framework that uses a required-reason API"
    assert failed(rs) == {name} and "FileTimestamp" in rs[name].detail
    (app / "PrivacyInfo.xcprivacy").unlink()
    assert "privacy: app manifest (no tracking, file timestamp, boot time, disk space)" in failed(results(app))


def test_validate_store_settings_in_archive(tmp_path):
    app = synthetic_app(tmp_path)
    (app / "pocketnode_settings.json").write_text(json.dumps({"edition": "store", "debug": True}))
    arc = tmp_path / "PocketNode.xcarchive/Products/Applications"
    arc.mkdir(parents=True)
    shutil.move(str(app), arc / "PocketNode.app")
    b, rs = validate.validate(tmp_path / "PocketNode.xcarchive")
    assert b.archive
    assert {r.name for r in rs if r.ok is False} == {
        "leftovers: pocketnode_settings.json is the store edition's (debug off)"}
    lines = []
    assert validate.main(tmp_path / "PocketNode.xcarchive", out=lines.append) == 1
    assert any(line.startswith("FAIL  leftovers: pocketnode_settings.json") for line in lines)
    assert "1 failed" in lines[-1]


def test_validate_skips_device_checks_on_a_simulator_build(tmp_path, home):
    app = synthetic_app(tmp_path)
    # Turn it into a simulator build with a fat framework, like the simulator's Python.framework.
    cc(tmp_path, app / "PocketNode", "arm64-apple-ios26.0-simulator", "int main(void){return 0;}\n", "-exe")
    fw = app / "Frameworks/m._ext.framework/m._ext"
    arm = cc(tmp_path, tmp_path / "arm.dylib", SIM, STAT)
    x86 = cc(tmp_path, tmp_path / "x86.dylib", "x86_64-apple-ios17.0-simulator", STAT)
    subprocess.run(["lipo", "-create", str(arm), str(x86), "-output", str(fw)], check=True)
    info = plistlib.loads((app / "Info.plist").read_bytes())
    info.update(DTPlatformName="iphonesimulator", DTSDKName="iphonesimulator27.1",
                CFBundleSupportedPlatforms=["iPhoneSimulator"])
    (app / "Info.plist").write_bytes(plistlib.dumps(info))
    rs = results(app)
    for name in ("binaries: arm64 only", "binaries: LC_BUILD_VERSION platform iOS",
                 "binaries: LC_ENCRYPTION_INFO_64 present"):
        assert rs[name].ok is None and "simulator build" in rs[name].detail, name
    assert "1 of 2 binaries are fat" in rs["binaries: arm64 only"].detail
    assert failed(rs) == set(), {n: rs[n].detail for n in failed(rs)}
    # `pocketnode app validate --sim` finds the store simulator build when there's no archive
    built = app_mod.product("iphonesimulator", "store")
    built.parent.mkdir(parents=True)
    shutil.move(str(app), built)
    assert validate.default_path("iphonesimulator") == built
    assert validate.default_path() == built
    assert validate.default_path("iphoneos") is None
