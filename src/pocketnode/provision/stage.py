"""Stage Python packages from this Mac's environment for the iOS app.

Pure-Python files are copied as they are. Every Mach-O binary (extension
modules and the dylibs they bundle) is retagged from macOS to iOS: thinned to
arm64, system framework paths shortened to the iOS layout, the build-version
load command switched to iOS (or the iOS simulator), and wrapped as a
framework the way CPython's iOS loader expects (a `.fwork` pointer where the
`.so` was). Xcode signs the frameworks when it builds the app.

The result is the exact versions the Mac runs, so the router's version checks
always match. Each binary is also checked statically against the iOS SDK, and
anything that can't load is reported (and left out) instead of failing on the
phone.
"""

from __future__ import annotations

import importlib.metadata as md
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from packaging.markers import default_environment

from packaging.requirements import Requirement

from .. import progress as progress_mod
from ..sources import CONFIG_DIR, SIM_BUNDLE_ID
from . import shims

PLATFORM = {"iphoneos": "ios", "iphonesimulator": "iossim"}
SHIM_DIR = "_pocketnode_shims"
MINOS = "17.0"
STAGE_ROOT = Path(os.environ.get("POCKETNODE_STAGE", CONFIG_DIR / "build"))
_SKIP_SUFFIXES = (".pyc", ".pyo", ".a", ".pyx", ".pxd", ".pxi", ".c", ".cpp", ".h", ".hpp", ".cc")
# The app's own sources (pocketnode/ios) are built into the app, not imported by it.
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "ios")
_MH_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
_DARWIN_SO = re.compile(r"\.cpython-(\d+)-darwin\.so$")


@dataclass
class Report:
    sdk: str
    dists: dict[str, str] = field(default_factory=dict)
    binaries: int = 0
    excluded: dict[str, str] = field(default_factory=dict)  # module -> reason
    warnings: list[str] = field(default_factory=list)
    notes: dict = field(default_factory=dict)
    seconds: float = 0.0


def _run(*cmd: str) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")
    return r.stdout


def sdk_version() -> str:
    return _run("xcrun", "--sdk", "iphoneos", "--show-sdk-version").strip()


def _norm(n: str) -> str:
    return re.sub(r"[-_.]+", "-", n).lower()


def closure(names: list[str]) -> list[md.Distribution]:
    """Installed distributions for `names` plus their runtime dependencies."""
    env = default_environment()
    by_name = {_norm(d.metadata["Name"]): d for d in md.distributions() if d.metadata["Name"]}
    out: dict[str, md.Distribution] = {}
    stack = [_norm(n) for n in names]
    while stack:
        n = stack.pop()
        if n in out:
            continue
        d = by_name.get(n)
        if d is None:
            raise LookupError(f"{n} isn't installed in {sys.prefix}")
        out[n] = d
        for r in d.requires or ():
            req = Requirement(r)
            if req.marker and not req.marker.evaluate({**env, "extra": ""}):
                continue
            if _norm(req.name) in by_name:
                stack.append(_norm(req.name))
    return list(out.values())


def _is_executable(p: Path) -> bool:
    out = _run("otool", "-hv", "-arch", "arm64", str(p)) if "arm64" in _run("lipo", "-archs", str(p)) else ""
    return " EXECUTE " in out


def is_macho(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(4) in _MH_MAGIC
    except OSError:
        return False


def _load_commands(p: Path) -> tuple[list[str], list[str], str | None]:
    """(dependent libraries, rpaths, own install name)."""
    deps, rpaths, ident = [], [], None
    lines = _run("otool", "-l", str(p)).splitlines()
    for i, l in enumerate(lines):
        l = l.strip()
        if l.startswith("cmd LC_LOAD_DYLIB") or l.startswith("cmd LC_LOAD_WEAK_DYLIB") or \
                l.startswith("cmd LC_REEXPORT_DYLIB") or l.startswith("cmd LC_ID_DYLIB"):
            for j in range(i + 1, i + 4):
                m = re.match(r"\s*name (.+) \(offset", lines[j])
                if m:
                    if l.startswith("cmd LC_ID_DYLIB"):
                        ident = m.group(1)
                    else:
                        deps.append(m.group(1))
                    break
        elif l.startswith("cmd LC_RPATH"):
            for j in range(i + 1, i + 4):
                m = re.match(r"\s*path (.+) \(offset", lines[j])
                if m:
                    rpaths.append(m.group(1))
                    break
    return deps, rpaths, ident


def _resolve(ref: str, binary: Path, rpaths: list[str]) -> Path | None:
    def sub(s: str) -> str:
        return s.replace("@loader_path", str(binary.parent)).replace("@executable_path", str(binary.parent))
    if ref.startswith("@rpath/"):
        for rp in rpaths:
            cand = Path(sub(rp)) / ref[len("@rpath/"):]
            if cand.exists():
                return cand.resolve()
        return None
    if ref.startswith("@loader_path"):
        cand = Path(sub(ref))
        return cand.resolve() if cand.exists() else None
    return None


def _ios_framework_path(ref: str) -> str:
    m = re.match(r"(/System/Library/Frameworks/[^/]+\.framework)/Versions/[^/]+/(.+)$", ref)
    return f"{m.group(1)}/{m.group(2)}" if m else ref


class Stager:
    def __init__(self, sdk: str, out: Path | None = None, bundle_id: str = SIM_BUNDLE_ID,
                 template: Path | None = None, inplace: bool = False):
        if sdk not in PLATFORM:
            raise ValueError(f"sdk must be one of {list(PLATFORM)}")
        self.sdk = sdk
        # In place: binaries stay where the package put them (plain .so and
        # .dylib files), for packages added to an installed app. Otherwise each
        # one is wrapped as a framework for the app bundle.
        self.inplace = inplace
        self.out = Path(out or STAGE_ROOT / sdk)
        self.pk = self.out / "app_packages"
        self.fw = self.out / "Frameworks"
        self.bundle_id = bundle_id
        self.template = template
        self.sdkver = sdk_version()
        self.report = Report(sdk)
        self.progress = progress_mod.NULL

    # ---------------------------------------------------------------- copy

    def stage(self, packages: list[str], extra_paths: dict[str, Path] | None = None,
              only=None, progress=None) -> Report:
        """only(dist) -> bool picks which distributions of the closure to stage (default: all).
        progress: a reporter (progress.Step or anything with its update/detail) told each phase
        and its count; None reports nothing."""
        p = self.progress = progress or progress_mod.NULL
        t0 = time.time()
        shutil.rmtree(self.out, ignore_errors=True)
        self.pk.mkdir(parents=True)
        if self.inplace:
            self.fw = self.pk / SHIM_DIR
        else:
            self.fw.mkdir(parents=True)
        dists = [d for d in closure(packages) if only is None or only(d)]
        for i, d in enumerate(dists):
            p.update(i, len(dists), detail="copying packages")
            self._copy_dist(d)
        p.update(len(dists), len(dists), detail="copying packages")
        for name, src in (extra_paths or {}).items():
            shutil.copytree(src, self.pk / name, ignore=_IGNORE)
        if "torch" in self.report.dists and (self.pk / "torch/lib/libtorch_cpu.dylib").exists():
            from . import torch_metal
            try:  # before retagging, which thins, re-signs and moves the dylib
                if torch_metal.install(self.pk, self.report.dists["torch"], self.sdk, progress=p):
                    self.report.notes["torch_metal"] = True
            except Exception as e:
                self.report.warnings.append(f"torch: no iOS GPU shaders ({e}); torch's Metal backend will be "
                                            "limited to operations that don't need them")
        self._convert_binaries()
        if "mlx" in self.report.dists and (self.pk / "mlx").exists():
            from . import mlx_metal
            try:
                if self.inplace:
                    shutil.copy2(mlx_metal.build(self.report.dists["mlx"], self.sdk, progress=p),
                                 self.pk / "mlx" / "lib" / "mlx.metallib")
                else:
                    mlx_metal.install(self.out, self.report.dists["mlx"], self.sdk, progress=p)
            except Exception as e:  # MLX still runs on its CPU backend without kernels
                self.report.warnings.append(f"mlx: no iOS GPU kernels ({e})")
        self.report.seconds = time.time() - t0
        (self.out / "stage-report.json").write_text(json.dumps(self.report.__dict__, indent=1))
        return self.report

    def _copy_dist(self, d: md.Distribution) -> None:
        name = d.metadata["Name"]
        self.report.dists[name] = d.version
        for f in d.files or ():
            rel = str(f)
            if rel.startswith("..") or "__pycache__" in rel or rel.endswith(_SKIP_SUFFIXES):
                continue
            parts = f.parts
            if len(parts) > 2 and parts[1] == "tests":  # pkg/tests/... isn't needed at runtime
                continue
            src = Path(d.locate_file(f))
            if not src.is_file():
                continue
            if rel.endswith(".pth") and "__editable__" in rel:
                continue
            dst = self.pk / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # --------------------------------------------------------- binaries

    def _convert_binaries(self) -> None:
        prog = self.progress
        prog.detail("finding binaries")
        bins = [p for p in self.pk.rglob("*") if p.is_file() and not p.is_symlink() and is_macho(p)]
        # Executables (helper tools like torch_shm_manager) can never run on iOS:
        # no child processes. Keep a placeholder so existence checks still pass.
        tick = progress_mod.counter(prog, len(bins), "reading binaries")

        def read(p):
            try:
                return _is_executable(p), _load_commands(p)
            finally:
                tick()

        with ThreadPoolExecutor(8) as pool:
            got = dict(zip(bins, pool.map(read, bins)))
        for p in [b for b in bins if got[b][0]]:
            p.write_text("pocketnode: executables can't run on iOS (no child processes)\n")
            bins.remove(p)
        self.report.binaries = len(bins)
        names: dict[Path, str] = {}
        for p in bins:
            names[p.resolve()] = self._framework_name(p)
        info = {p: got[p][1] for p in bins}
        problems = self._check(bins)
        tick = progress_mod.counter(prog, len(bins), "retagging")
        if self.inplace:
            def one(p):
                try:
                    self._retag_inplace(p, info[p], problems.get(p))
                finally:
                    tick()
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(one, bins))
            return
        # Resolve bundled-library references before anything moves.
        links: dict[Path, dict[str, str]] = {}
        for p in bins:
            deps, rpaths, _ = info[p]
            links[p] = {}
            for ref in deps:
                target = _resolve(ref, p, rpaths)
                if target is not None and target in names:
                    links[p][ref] = names[target]
        def one(p):
            try:
                self._retag(p, info[p], names, problems.get(p), links[p])
            finally:
                tick()

        with ThreadPoolExecutor(8) as pool:
            list(pool.map(one, bins))
        for fw in [*self.fw.glob("pocketnode_shim_*.framework"), *self.fw.glob("pocketnode_absent_*.framework")]:
            self._write_plist(fw, fw.name[: -len(".framework")])
        # Libraries some packages load by path with ctypes (torch, lightgbm, ...)
        # now live in Frameworks; the worker redirects those loads through this map.
        moved = {str(p.relative_to(self.pk)): f"Frameworks/{names[p.resolve()]}.framework/{names[p.resolve()]}"
                 for p in bins if p.suffix != ".so" and names[p.resolve()] not in self.report.excluded}
        (self.pk / "pocketnode_dylib_map.json").write_text(json.dumps(moved, indent=0))

    def _framework_name(self, p: Path) -> str:
        rel = p.relative_to(self.pk)
        if rel.as_posix() == "torch/lib/libtorch_cpu.dylib":
            # torch finds its Metal shaders in the loaded image whose path contains this.
            return "libtorch_cpu.dylib"
        stem = rel.name.split(".")[0] if p.suffix == ".so" else rel.name.split(".dylib")[0]
        dotted = ".".join([*(x.lstrip(".") for x in rel.parent.parts), stem])
        return re.sub(r"[^A-Za-z0-9_.]", "_", dotted)

    def _check(self, bins: list[Path]) -> dict[Path, str]:
        try:
            from . import symcheck
            # Always the device SDK: it's the real target, and the simulator
            # SDK's libSystem stubs give false misses.
            symcheck.use_sdk("iphoneos")
        except ImportError:
            self.report.warnings.append("PyYAML missing: skipped the static iOS symbol check")
            return {}
        out = {}
        tick = progress_mod.counter(self.progress, len(bins), "checking against the iOS SDK")

        def one(p):
            try:
                return check(p)
            finally:
                tick()

        def check(p):
            try:
                r = symcheck.analyze(str(p))
            except Exception as e:
                return p, f"check failed: {e}"
            # System frameworks iOS lacks (AppKit, Carbon, ...) back peripheral
            # features (GUI, clipboard): stand-ins let the rest load.
            # Missing plain dylibs (libcurl, libffi) are core to their module.
            weak = {l for l in r["missing_libs"] if l.startswith("/System/Library/Frameworks/")}
            bad = [l for l in r["missing_libs"] if l not in weak]
            by_lib: dict[str, list[str]] = {}
            for sym, lib in r["missing_syms"]:
                by_lib.setdefault(lib, []).append(sym)
            if not bad and (weak or by_lib) and all(shims.can_shim(v, l) for l, v in by_lib.items()):
                absent = {l: r["absent_imports"].get(l, []) for l in weak}
                return p, {"shim": by_lib, "absent": absent}  # fixable
            syms = [s for s, _ in r["missing_syms"]]
            if bad or syms:
                return p, "; ".join(filter(None, [
                    f"links {', '.join(bad)} (not on iOS)" if bad else "",
                    f"needs {', '.join(syms[:6])}{'…' if len(syms) > 6 else ''} (not on iOS)" if syms else "",
                ]))
            return p, None

        with ThreadPoolExecutor(8) as pool:
            for p, why in pool.map(one, bins):
                if why:
                    out[p] = why
        return out

    def _retag(self, p: Path, lc, names: dict[Path, str], problem, links: dict[str, str]) -> None:
        deps, rpaths, ident = lc
        fw = names[p.resolve()]
        rel = p.relative_to(self.pk)
        shimmed: dict[str, str] = {}
        if isinstance(problem, dict):
            for lib, syms in problem["shim"].items():
                shimmed[lib] = shims.build(lib, syms, self.sdk, MINOS, self.fw)
                self.report.warnings.append(f"{fw}: shimmed {', '.join(syms)} from {lib}")
            for lib, syms in problem["absent"].items():
                shimmed[lib] = shims.build_absent(lib, syms, self.sdk, MINOS, self.fw)
                self.report.warnings.append(f"{fw}: {Path(lib).name} isn't on iOS; stood in for "
                                            f"{', '.join(syms) or 'nothing'} (features using it are unavailable)")
            problem = None
        if problem:
            # Leave the module out: importing it raises ImportError, which the
            # worker reports so the Mac runs the task instead.
            self.report.excluded[fw] = problem
            p.unlink()
            return
        _run("codesign", "--remove-signature", str(p)) if _signed(p) else None
        if "arm64" in _run("lipo", "-archs", str(p)).split() and len(_run("lipo", "-archs", str(p)).split()) > 1:
            _run("lipo", "-thin", "arm64", str(p), "-output", str(p))
        args = []
        for ref in deps:
            new = _ios_framework_path(ref)
            if new in shimmed or ref in shimmed:
                name = shimmed.get(new) or shimmed[ref]
                new = f"@rpath/{name}.framework/{name}"
            if ref in links:
                new = f"@rpath/{links[ref]}.framework/{links[ref]}"
            if new != ref:
                args += ["-change", ref, new]
        if ident is not None:
            args += ["-id", f"@rpath/{fw}.framework/{fw}"]
        if "@executable_path/Frameworks" not in rpaths:
            args += ["-add_rpath", "@executable_path/Frameworks"]
        if args:
            _run("install_name_tool", *args, str(p))
        _run("xcrun", "vtool", "-set-build-version", PLATFORM[self.sdk], MINOS, self.sdkver,
             "-replace", "-output", str(p), str(p))
        fix_zerofill_offsets(p)
        align_string_table(p)
        # Into Frameworks/<dotted name>.framework/<dotted name>
        dest = self.fw / f"{fw}.framework"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), dest / fw)
        self._write_plist(dest, fw)
        if p.suffix == ".so":
            fname = _DARWIN_SO.sub(rf".cpython-\1-{self.sdk}.so", p.name)
            fwork = p.with_name(fname[: -len(".so")] + ".fwork")
            fwork.write_text(f"Frameworks/{fw}.framework/{fw}")
            (dest / f"{fw}.origin").write_text(str(Path("app_packages") / rel.parent / fwork.name))

    def _retag_inplace(self, p: Path, lc, problem) -> None:
        """Retag a binary where it is. The package keeps its layout, so the
        references between its own libraries (@loader_path, @rpath) stay valid."""
        deps, rpaths, ident = lc
        rel = str(p.relative_to(self.pk))
        shimmed: dict[str, str] = {}
        if isinstance(problem, dict):
            for lib, syms in problem["shim"].items():
                shimmed[lib] = shims.build(lib, syms, self.sdk, MINOS, self.fw)
                self.report.warnings.append(f"{rel}: shimmed {', '.join(syms)} from {lib}")
            for lib, syms in problem["absent"].items():
                shimmed[lib] = shims.build_absent(lib, syms, self.sdk, MINOS, self.fw)
                self.report.warnings.append(f"{rel}: {Path(lib).name} isn't on iOS; stood in for "
                                            f"{', '.join(syms) or 'nothing'} (features using it are unavailable)")
            problem = None
        if problem:
            self.report.excluded[rel] = problem
            p.unlink()
            return
        _run("codesign", "--remove-signature", str(p)) if _signed(p) else None
        archs = _run("lipo", "-archs", str(p)).split()
        if "arm64" in archs and len(archs) > 1:
            _run("lipo", "-thin", "arm64", str(p), "-output", str(p))
        args = []
        for ref in deps:
            new = _ios_framework_path(ref)
            name = shimmed.get(new) or shimmed.get(ref)
            if name:
                target = self.fw / f"{name}.framework" / name
                new = "@loader_path/" + os.path.relpath(target, p.parent)
            if new != ref:
                args += ["-change", ref, new]
        if args:
            _run("install_name_tool", *args, str(p))
        _run("xcrun", "vtool", "-set-build-version", PLATFORM[self.sdk], MINOS, self.sdkver,
             "-replace", "-output", str(p), str(p))
        fix_zerofill_offsets(p)
        align_string_table(p)
        if p.suffix == ".so":
            p.rename(p.with_name(_DARWIN_SO.sub(rf".cpython-\1-{self.sdk}.so", p.name)))

    def _write_plist(self, dest: Path, fw: str) -> None:
        plist = {
            "CFBundleDevelopmentRegion": "en", "CFBundleExecutable": fw,
            "CFBundleIdentifier": re.sub(r"[^A-Za-z0-9.-]", "-", f"{self.bundle_id}.{fw}"),
            "CFBundleInfoDictionaryVersion": "6.0", "CFBundlePackageType": "FMWK",
            "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
            "CFBundleSupportedPlatforms": ["iPhoneSimulator" if self.sdk == "iphonesimulator" else "iPhoneOS"],
            "MinimumOSVersion": MINOS,
        }
        (dest / "Info.plist").write_bytes(plistlib.dumps(plist))


_ZEROFILL_TYPES = {0x1, 0xC, 0x12}  # S_ZEROFILL, S_GB_ZEROFILL, S_THREAD_LOCAL_ZEROFILL


def fix_zerofill_offsets(p: Path) -> int:
    """Zero the file offset of zero-fill sections (they have no file contents).

    Older linkers left a nonzero offset on __thread_bss. dyld tolerates that
    for macOS binaries but rejects it on iOS ("has a zero-fill section type,
    but offset field is not zero"). Returns the number of sections fixed.
    """
    import struct
    data = bytearray(p.read_bytes())
    if data[:4] != b"\xcf\xfa\xed\xfe":
        return 0
    ncmds = struct.unpack_from("<I", data, 16)[0]
    off, fixed = 32, 0
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, off)
        if cmd == 0x19:  # LC_SEGMENT_64
            nsects = struct.unpack_from("<I", data, off + 64)[0]
            sec = off + 72
            for _ in range(nsects):
                flags = struct.unpack_from("<I", data, sec + 64)[0]
                if flags & 0xFF in _ZEROFILL_TYPES and struct.unpack_from("<I", data, sec + 48)[0]:
                    struct.pack_into("<I", data, sec + 48, 0)
                    fixed += 1
                sec += 80
        off += size
    if fixed:
        p.write_bytes(data)
    return fixed


# (cmd, [byte offsets of file-offset fields]) for load commands pointing into __LINKEDIT
_LINKEDIT_FIELDS = {
    0x2: [8, 16],                         # LC_SYMTAB: symoff, stroff
    0xB: [32, 40, 48, 56, 64, 72],        # LC_DYSYMTAB
    0x22: [8, 16, 24, 32, 40],            # LC_DYLD_INFO
    0x80000022: [8, 16, 24, 32, 40],      # LC_DYLD_INFO_ONLY
    **{c: [8] for c in (0x1D, 0x1E, 0x26, 0x29, 0x2B, 0x2E, 0x80000033, 0x80000034)},  # linkedit_data
}


def align_string_table(p: Path) -> int:
    """Pad the symbol string table to an 8-byte boundary.

    Some macOS wheels' bundled dylibs (sklearn's libomp) have a string pool at
    a 4-byte offset; iOS dyld refuses them ("mis-aligned LINKEDIT string
    pool"). Inserts padding and shifts every offset at or past it.
    """
    import struct
    data = bytearray(p.read_bytes())
    if data[:4] != b"\xcf\xfa\xed\xfe":
        return 0
    ncmds = struct.unpack_from("<I", data, 16)[0]
    cmds, off = [], 32
    stroff = None
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, off)
        cmds.append((cmd, off))
        if cmd == 0x2:
            stroff = struct.unpack_from("<I", data, off + 16)[0]
        off += size
    if stroff is None or stroff % 8 == 0:
        return 0
    pad = 8 - stroff % 8
    for cmd, at in cmds:
        for f in _LINKEDIT_FIELDS.get(cmd, ()):
            v = struct.unpack_from("<I", data, at + f)[0]
            if v >= stroff:
                struct.pack_into("<I", data, at + f, v + pad)
        if cmd == 0x19 and bytes(data[at + 8: at + 24]).rstrip(b"\0") == b"__LINKEDIT":
            vmsize, fileoff, filesize = struct.unpack_from("<QQQ", data, at + 32)
            filesize += pad
            vmsize = max(vmsize, (filesize + 0x3FFF) & ~0x3FFF)
            struct.pack_into("<QQQ", data, at + 32, vmsize, fileoff, filesize)
    data[stroff:stroff] = b"\0" * pad
    p.write_bytes(data)
    return pad


def _signed(p: Path) -> bool:
    return subprocess.run(["codesign", "-d", str(p)], capture_output=True).returncode == 0


def stage(packages: list[str], sdk: str = "iphonesimulator", out: Path | None = None,
          bundle_id: str = SIM_BUNDLE_ID, progress=None) -> Report:
    import pocketnode
    src = Path(pocketnode.__file__).parent
    s = Stager(sdk, out, bundle_id)
    return s.stage(sorted(set(packages) | {"cloudpickle"}), extra_paths={"pocketnode": src}, progress=progress)


def refresh_pocketnode(sdk: str = "iphonesimulator") -> None:
    """Update just the staged copy of pocketnode itself (fast inner loop while developing)."""
    import pocketnode
    dst = STAGE_ROOT / sdk / "app_packages" / "pocketnode"
    shutil.copytree(Path(pocketnode.__file__).parent, dst, dirs_exist_ok=True, ignore=_IGNORE)
