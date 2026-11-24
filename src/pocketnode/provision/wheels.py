"""Stage the App Store edition's fixed package set from iOS wheels.

The sideloaded edition retags the Mac's own macOS binaries (stage.py). The
store edition can't: every binary in it has to be a real iOS build. So its
compiled packages come from cp313 iOS wheels (PyPI, BeeWare's index, or a local
wheelhouse), installed for the device or the simulator, never built from source.

Each binary becomes a framework the way CPython's iOS docs describe
(https://docs.python.org/3.13/using/ios.html, "Binary extension modules"):
`Frameworks/<dotted.module>.framework/<dotted.module>` with an Info.plist and an
`.origin` file, and a `.fwork` file where the `.so` was. Nothing is retagged or
patched; libraries a wheel bundles (a `.dylibs` folder) are wrapped too, with
the references to them pointed at the frameworks.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from email.parser import HeaderParser
from pathlib import Path

from ..sources import CONFIG_DIR
from . import stage as stage_mod
from .validate import binary_problem, is_macho, macho

BEEWARE_INDEX = "https://pypi.anaconda.org/beeware/simple"
PACKAGES_FILE = Path(__file__).resolve().parent.parent / "ios" / "Store" / "packages.txt"
UV_PLATFORM = {"iphoneos": "arm64-apple-ios", "iphonesimulator": "arm64-apple-ios-simulator"}
PYTHON = "3.13"  # the phone's CPython (tools.PYTHON_SUPPORT)
# pocketnode's own subpackages the phone never uses.
_POCKETNODE_SKIP = {"ios", "provision", "__pycache__", "tests"}


class WheelError(RuntimeError):
    pass


def wheelhouse(cfg: dict | None = None) -> Path:
    cfg = cfg or {}
    return Path(cfg.get("store_wheelhouse") or CONFIG_DIR / "wheelhouse").expanduser()


def packages_file(cfg: dict | None = None) -> Path:
    cfg = cfg or {}
    return Path(cfg.get("store_packages_file") or PACKAGES_FILE).expanduser()


def read_requirements(path: Path) -> list[str]:
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    try:
        import uv  # the `uv` package on PyPI ships the binary
        return uv.find_uv_bin()
    except Exception:  # noqa: BLE001
        raise WheelError("building the App Store edition needs uv (https://docs.astral.sh/uv/) to install iOS "
                         "wheels: `brew install uv` or `pip install uv`") from None


def _req_name(req: str) -> str:
    return re.split(r"[\s<>=!~;\[@]", req, maxsplit=1)[0]


def _no_wheel(output: str, requirements: list[str], sdk: str, house: Path) -> str:
    names = re.findall(r"Wheels are required for `([^`]+)`", output)
    if not names:
        names = [n for n in map(_req_name, requirements)
                 if re.search(rf"\b{re.escape(n)}\b", output, re.I)]
    names = list(dict.fromkeys(names))
    tag = "ios_*_arm64_iphoneos" if sdk == "iphoneos" else "ios_*_arm64_iphonesimulator"
    if names:
        what = ", ".join(names)
        return (f"no cp313 iOS wheel ({tag}) found for {what}, and the App Store edition never builds from "
                f"source. Put a wheel for it in the wheelhouse ({house}, or the store_wheelhouse setting) or "
                f"remove it from the package list. uv said:\n{output.strip()[-1500:]}")
    return f"installing the iOS wheels failed:\n{output.strip()[-2000:]}"


def install(requirements: list[str], sdk: str, target: Path, house: Path | None = None,
            indexes: list[str] | None = None) -> None:
    """Install `requirements` and their dependencies into `target` from iOS binary wheels."""
    cmd = [_uv(), "pip", "install", "--target", str(target), "--python-platform", UV_PLATFORM[sdk],
           "--python-version", PYTHON, "--python", sys.executable, "--only-binary", ":all:",
           "--index-strategy", "unsafe-best-match", "--no-progress"]
    for url in (indexes if indexes is not None else [BEEWARE_INDEX]):
        cmd += ["--extra-index-url", url]
    if house is not None and Path(house).is_dir():
        cmd += ["--find-links", str(house)]
    cmd += requirements
    # From an empty folder, so no project's uv settings apply.
    target.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=target)
    if r.returncode:
        raise WheelError(_no_wheel(r.stdout + r.stderr, requirements, sdk, Path(house or wheelhouse())))


def _prune(pk: Path) -> None:
    """Drop what the phone never runs: scripts, tests, headers, static libraries, sources,
    and dotfiles (uv's .lock; App Store validation rejects hidden files)."""
    shutil.rmtree(pk / "bin", ignore_errors=True)
    for p in sorted(pk.rglob("*"), key=lambda p: -len(p.parts)):
        if p.is_dir() and p.name in ("__pycache__", "tests") and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        elif (p.is_file() or p.is_symlink()) and (p.name.endswith(stage_mod._SKIP_SUFFIXES) or p.name.startswith(".")):
            p.unlink()
    _remove_empty_dirs(pk)


def _remove_empty_dirs(root: Path) -> None:
    for p in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
        if p.is_dir() and not p.is_symlink() and not any(p.iterdir()):
            p.rmdir()


def copy_pocketnode(pk: Path) -> dict[str, str]:
    """Copy pocketnode (without what only the Mac uses) and cloudpickle into `pk`. Returns their versions."""
    import importlib.metadata as md

    import cloudpickle
    import pocketnode

    def ignore(d, names):
        out = {n for n in names if n in ("__pycache__", "tests") or n.endswith(stage_mod._SKIP_SUFFIXES)
               or n == ".DS_Store"}
        if Path(d).resolve() == Path(pocketnode.__file__).resolve().parent:
            out |= {n for n in names if n in _POCKETNODE_SKIP}
        return out

    for mod in (pocketnode, cloudpickle):
        src = Path(mod.__file__).resolve().parent
        dst = pk / src.name
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, ignore=ignore)
    return {"pocketnode": md.version("pocketnode"), "cloudpickle": md.version("cloudpickle")}


def _dists(pk: Path) -> dict[str, str]:
    out = {}
    for d in sorted(pk.glob("*.dist-info")):
        try:
            meta = HeaderParser().parsestr((d / "METADATA").read_text(errors="replace"))
            out[meta["Name"]] = meta["Version"]
        except OSError:
            continue
    return out


class StoreStager(stage_mod.Stager):
    """Wraps real iOS binaries as frameworks, reusing stage.Stager's naming and Info.plist."""

    def __init__(self, sdk: str, out: Path, bundle_id: str):
        super().__init__(sdk, out, bundle_id)

    def wrap_all(self) -> int:
        bins = [p for p in self.pk.rglob("*") if p.is_file() and not p.is_symlink() and is_macho(p)]
        # Fat binaries (a simulator wheel may carry x86_64 too) are thinned to arm64 first.
        for p in bins:
            archs = [s.arch for s in macho(p)]
            if len(archs) > 1 and "arm64" in archs:
                stage_mod._run("lipo", "-thin", "arm64", str(p), "-output", str(p))
        problems = [f"{p.relative_to(self.pk)} {why}" for p in bins if (why := binary_problem(p, self.sdk))]
        if problems:
            raise WheelError("these binaries aren't real builds for "
                             f"{'the iOS device' if self.sdk == 'iphoneos' else 'the iOS simulator'}, so the App "
                             "Store edition can't use them:\n  " + "\n  ".join(problems))
        names: dict[Path, str] = {}
        seen: dict[str, Path] = {}
        for p in bins:
            n = self._framework_name(p)
            if n in seen:
                raise WheelError(f"{p.relative_to(self.pk)} and {seen[n].relative_to(self.pk)} would both "
                                 f"become {n}.framework")
            seen[n] = p
            names[p.resolve()] = n
        # References between a wheel's own binaries (.so -> bundled .dylib) are
        # pointed at the frameworks before anything moves.
        for p in bins:
            deps, rpaths, ident = stage_mod._load_commands(p)
            args = []
            for ref in deps:
                t = stage_mod._resolve(ref, p, rpaths)
                if t is not None and t in names and t != p.resolve():
                    args += ["-change", ref, f"@rpath/{names[t]}.framework/{names[t]}"]
            if p.suffix != ".so" and ident is not None:
                args += ["-id", f"@rpath/{names[p.resolve()]}.framework/{names[p.resolve()]}"]
            if args:
                stage_mod._run("install_name_tool", *args, str(p))
        for p in bins:
            self._wrap(p, names[p.resolve()])
        _remove_empty_dirs(self.pk)  # a wheel's .dylibs folder, now in Frameworks
        self.report.binaries = len(bins)
        return len(bins)

    def _wrap(self, p: Path, fw: str) -> None:
        rel = p.relative_to(self.pk)
        dest = self.fw / f"{fw}.framework"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), dest / fw)
        self._write_plist(dest, fw)
        if p.suffix == ".so":
            fwork = p.with_name(p.name[: -len(".so")] + ".fwork")
            fwork.write_text(f"Frameworks/{fw}.framework/{fw}")
            (dest / f"{fw}.origin").write_text(str(Path("app_packages") / rel.parent / fwork.name))
        else:
            self.report.notes.setdefault("wrapped_libraries", []).append(str(rel))


def stage_store(requirements: list[str], sdk: str, out: Path, bundle_id: str,
                wheelhouse: Path | None = None, indexes: list[str] | None = None) -> stage_mod.Report:
    """Install `requirements` from iOS wheels into `<out>/app_packages`, wrap every
    binary as a framework in `<out>/Frameworks`, add pocketnode and cloudpickle,
    and write `<out>/stage-report.json`."""
    t0 = time.time()
    out = Path(out)
    shutil.rmtree(out, ignore_errors=True)
    s = StoreStager(sdk, out, bundle_id)
    s.fw.mkdir(parents=True)
    if requirements:
        install(requirements, sdk, s.pk, wheelhouse, indexes)
    else:
        s.pk.mkdir(parents=True)
    _prune(s.pk)
    s.report.dists = _dists(s.pk)
    s.wrap_all()
    s.report.dists.update(copy_pocketnode(s.pk))
    s.report.notes.update({"edition": "store", "requirements": list(requirements),
                           "wheelhouse": str(wheelhouse) if wheelhouse else None})
    s.report.seconds = time.time() - t0
    (out / "stage-report.json").write_text(json.dumps(s.report.__dict__, indent=1))
    return s.report


def refresh_pocketnode(out: Path) -> None:
    """Update just the staged copy of pocketnode (and cloudpickle)."""
    copy_pocketnode(Path(out) / "app_packages")
