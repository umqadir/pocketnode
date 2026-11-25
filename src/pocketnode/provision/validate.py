"""Check a built App Store edition (.xcarchive or .app) against what App Store
Connect and App Review look at, before anything is uploaded.

    pocketnode app validate [PATH]

One PASS/FAIL/SKIP line per check, then a summary; any FAIL makes the exit
status 1. Checks that only apply to device builds (encryption info) are skipped
for simulator builds. Nothing here signs, uploads or changes the bundle.
"""

from __future__ import annotations

import os
import plistlib
import re
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

# ------------------------------------------------------------------ Mach-O

CPU_ARM64, CPU_X86_64 = 0x0100000C, 0x01000007
MH_EXECUTE, MH_BUNDLE, MH_DYLIB = 0x2, 0x8, 0x6
FILETYPES = {0x1: "object", MH_EXECUTE: "executable", MH_DYLIB: "dylib", MH_BUNDLE: "bundle"}
PLATFORMS = {1: "macOS", 2: "iOS", 6: "Mac Catalyst", 7: "iOS Simulator"}
SDK_PLATFORM = {"iphoneos": 2, "iphonesimulator": 7}
_LOAD = {0xC: "load", 0x80000018: "weak", 0x8000001F: "reexport", 0x20: "lazy", 0x80000023: "upward"}
MAGICS = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf",
          b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"}


@dataclass
class Slice:
    arch: str
    filetype: int = 0
    platform: int | None = None
    minos: str | None = None
    sdk: str | None = None
    encryption: bool = False   # has LC_ENCRYPTION_INFO_64 (only Apple's linker adds it, for iOS)
    deps: list[tuple[str, str]] = field(default_factory=list)  # (install name, kind)
    rpaths: list[str] = field(default_factory=list)
    ident: str | None = None
    text: int = 0              # bytes of the __TEXT segment
    methnames: list[str] = field(default_factory=list)  # __TEXT,__objc_methname


def is_macho(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    if head[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
        # Java class files share the fat magic; a fat header has a small arch count there.
        return struct.unpack(">I", head[4:8])[0] < 32
    return head[:4] in MAGICS


def _arch(cpu: int, sub: int) -> str:
    if cpu == CPU_ARM64:
        return "arm64e" if sub & 0xFFFFFF == 2 else "arm64"
    if cpu == CPU_X86_64:
        return "x86_64"
    return f"cpu{cpu:#x}"


def _version(v: int) -> str:
    return f"{v >> 16}.{(v >> 8) & 0xFF}" + (f".{v & 0xFF}" if v & 0xFF else "")


def _cstr(b: bytes, at: int) -> str:
    end = b.find(b"\0", at)
    return b[at: end if end >= 0 else len(b)].decode("utf-8", "replace")


def _thin(data: bytes, base: int = 0) -> Slice:
    if data[base: base + 4] != b"\xcf\xfa\xed\xfe":
        return Slice("32-bit or unknown")
    cpu, sub, ftype, ncmds = struct.unpack_from("<iiII", data, base + 4)
    s = Slice(_arch(cpu & 0xFFFFFFFF, sub), ftype)
    off = base + 32
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, off)
        if cmd == 0x32:  # LC_BUILD_VERSION
            s.platform, mn, sd = struct.unpack_from("<III", data, off + 8)
            s.minos, s.sdk = _version(mn), _version(sd)
        elif cmd in (0x25, 0x2F) and s.platform is None:  # LC_VERSION_MIN_IPHONEOS / _TVOS (old linkers)
            mn, sd = struct.unpack_from("<II", data, off + 8)
            s.platform, s.minos, s.sdk = (2 if cmd == 0x25 else 3), _version(mn), _version(sd)
        elif cmd == 0x2C:  # LC_ENCRYPTION_INFO_64
            s.encryption = True
        elif cmd in _LOAD:
            s.deps.append((_cstr(data, off + struct.unpack_from("<I", data, off + 8)[0]), _LOAD[cmd]))
        elif cmd == 0xD:  # LC_ID_DYLIB
            s.ident = _cstr(data, off + struct.unpack_from("<I", data, off + 8)[0])
        elif cmd == 0x8000001C:  # LC_RPATH
            s.rpaths.append(_cstr(data, off + struct.unpack_from("<I", data, off + 8)[0]))
        elif cmd == 0x19:  # LC_SEGMENT_64
            seg = data[off + 8: off + 24].rstrip(b"\0")
            if seg == b"__TEXT":
                s.text = struct.unpack_from("<Q", data, off + 48)[0]
                nsects = struct.unpack_from("<I", data, off + 64)[0]
                for i in range(nsects):
                    sec = off + 72 + 80 * i
                    if data[sec: sec + 16].rstrip(b"\0") == b"__objc_methname":
                        size_, foff = struct.unpack_from("<QI", data, sec + 40)[0], \
                            struct.unpack_from("<I", data, sec + 48)[0]
                        raw = data[base + foff: base + foff + size_]
                        s.methnames = [x.decode("utf-8", "replace") for x in raw.split(b"\0") if x]
        off += size
    return s


def macho(p: Path) -> list[Slice]:
    """The slices of a Mach-O file (one for a thin file)."""
    data = Path(p).read_bytes()
    magic = data[:4]
    if magic in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
        n = struct.unpack_from(">I", data, 4)[0]
        out = []
        for i in range(n):
            if magic == b"\xca\xfe\xba\xbe":
                cpu, sub, off, _size, _align = struct.unpack_from(">iiIII", data, 8 + 20 * i)
            else:
                cpu, sub, off, _size, _align, _r = struct.unpack_from(">iiQQII", data, 8 + 32 * i)
            sl = _thin(data, off)
            sl.arch = _arch(cpu & 0xFFFFFFFF, sub)
            out.append(sl)
        return out
    return [_thin(data)]


def binary_problem(p: Path, sdk: str) -> str | None:
    """Why the (thin) binary `p` isn't a real build for `sdk`, or None if it is."""
    sl = macho(p)
    if [s.arch for s in sl] != ["arm64"]:
        return f"has the architectures {', '.join(s.arch for s in sl)}; it must be arm64 only"
    s = sl[0]
    want = SDK_PLATFORM[sdk]
    if s.platform != want:
        got = PLATFORMS.get(s.platform, f"platform {s.platform}") if s.platform is not None else "no platform"
        return (f"is built for {got} (LC_BUILD_VERSION platform {s.platform}), not "
                f"{PLATFORMS[want]} ({want})")
    if sdk == "iphoneos" and not s.encryption:
        return "has no LC_ENCRYPTION_INFO_64 load command, so it wasn't linked for iOS by Apple's linker"
    return None


# ------------------------------------------------------------- symbols

_IMPORT = re.compile(r"\s+(?:0x[0-9a-fA-F]+\s+)?(\S+)\s+(\[weak-import\]\s+)?\(from (.+)\)")


def imports(p: Path) -> list[tuple[str, bool, str]]:
    """(symbol, weak, library leaf name as dyld_info prints it) for every imported symbol."""
    r = subprocess.run(["dyld_info", "-arch", "arm64", "-imports", str(p)], capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        m = _IMPORT.match(line)
        if m:
            out.append((m.group(1), bool(m.group(2)), m.group(3)))
    return out


def exports(p: Path) -> set[str]:
    r = subprocess.run(["dyld_info", "-arch", "arm64", "-exports", str(p)], capture_output=True, text=True)
    out = set()
    for line in r.stdout.splitlines():
        m = re.match(r"\s+0x[0-9a-fA-F]+\s+(\S+)", line)
        if m:
            out.add(m.group(1))
    return out


# Required-reason APIs (https://developer.apple.com/documentation/bundleresources/describing-use-of-required-reason-api)
REASON_SYMBOLS = {
    **{f"_{s}": ("NSPrivacyAccessedAPICategoryFileTimestamp", "C617.1") for s in
       ("stat", "fstat", "lstat", "fstatat", "getattrlist", "fgetattrlist", "getattrlistbulk", "getattrlistat")},
    "_mach_absolute_time": ("NSPrivacyAccessedAPICategorySystemBootTime", "35F9.1"),
    **{f"_{s}": ("NSPrivacyAccessedAPICategoryDiskSpace", "E174.1") for s in
       ("statfs", "fstatfs", "statvfs", "fstatvfs")},
}
CATEGORIES = dict(sorted(set(REASON_SYMBOLS.values())))
HOSTILE_MAIN = {"_class_addMethod", "_class_replaceMethod", "_imp_implementationWithBlock",
                "_method_setImplementation"}


def manifest_categories(p: Path) -> dict[str, set[str]] | None:
    """{category: reasons} of a PrivacyInfo.xcprivacy, None if it doesn't parse."""
    try:
        d = plistlib.loads(p.read_bytes())
        return {e["NSPrivacyAccessedAPIType"]: set(e.get("NSPrivacyAccessedAPITypeReasons", []))
                for e in d.get("NSPrivacyAccessedAPITypes", [])}
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------- the checks

@dataclass
class Result:
    name: str
    ok: bool | None   # None: skipped
    detail: str = ""
    info: bool = False  # listed for a person to look at; never fails


@dataclass
class Bundle:
    app: Path
    archive: bool
    info: dict
    sdk: str
    files: list[Path]
    machos: dict[Path, list[Slice]]
    main: Path
    imports: dict[Path, list[tuple[str, bool, str]]] = field(default_factory=dict)


def _limit(items: list[str], n: int = 6) -> str:
    return "; ".join(items[:n]) + (f"; and {len(items) - n} more" if len(items) > n else "")


def _check(name: str, problems: list[str], ok_detail: str = "") -> Result:
    if problems:
        return Result(name, False, f"{len(problems)} problem{'s' * (len(problems) > 1)}: {_limit(problems)}")
    return Result(name, True, ok_detail)


def find_app(path: Path) -> tuple[Path, bool]:
    path = Path(path)
    if path.suffix == ".xcarchive":
        apps = sorted((path / "Products" / "Applications").glob("*.app"))
        if not apps:
            raise FileNotFoundError(f"{path} has no Products/Applications/*.app")
        return apps[0], True
    if path.suffix == ".app" and path.is_dir():
        return path, False
    raise FileNotFoundError(f"{path} is neither an .xcarchive nor an .app")


def load(path: Path) -> Bundle:
    app, archive = find_app(path)
    info = plistlib.loads((app / "Info.plist").read_bytes())
    plat = str(info.get("DTPlatformName") or "").lower() or \
        ("iphonesimulator" if "iPhoneSimulator" in info.get("CFBundleSupportedPlatforms", []) else "iphoneos")
    files = []
    for root, dirs, names in os.walk(app):
        for n in names + [d for d in dirs if (Path(root) / d).is_symlink()]:
            files.append(Path(root) / n)
    main = app / info.get("CFBundleExecutable", app.stem)
    machos = {}
    for f in files:
        if not f.is_symlink() and f.is_file() and is_macho(f):
            machos[f] = macho(f)
    b = Bundle(app, archive, info, plat, files, machos, main)
    with ThreadPoolExecutor(8) as pool:
        for f, imp in zip(machos, pool.map(imports, machos)):
            b.imports[f] = imp
    return b


def _ver(v: str | None) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


def check_info_plist(b: Bundle) -> list[Result]:
    i = b.info
    r = []
    bid = i.get("CFBundleIdentifier", "")
    r.append(Result("Info.plist: bundle identifier", bool(re.fullmatch(r"[A-Za-z0-9.-]+", bid or "")), bid or "missing"))
    v, n = str(i.get("CFBundleShortVersionString", "")), str(i.get("CFBundleVersion", ""))
    vok = bool(re.fullmatch(r"\d+(\.\d+){0,2}", v)) and bool(re.fullmatch(r"\d+(\.\d+){0,2}", n))
    r.append(Result("Info.plist: versions", vok, f"{v or '?'} ({n or '?'})"))
    r.append(Result("Info.plist: MinimumOSVersion", bool(i.get("MinimumOSVersion")), i.get("MinimumOSVersion", "missing")))
    sdkname, platver = i.get("DTSDKName"), i.get("DTPlatformVersion")
    sok = bool(sdkname and platver) and _ver(platver)[:1] >= (26,)
    r.append(Result("Info.plist: built with the iOS 26+ SDK", sok, f"{sdkname or 'no DTSDKName'}, "
                    f"DTPlatformVersion {platver or 'missing'}"))
    caps = i.get("UIRequiredDeviceCapabilities", [])
    caps = list(caps) if isinstance(caps, (list, dict)) else []
    r.append(Result("Info.plist: requires arm64", "arm64" in caps, ", ".join(caps) or "no UIRequiredDeviceCapabilities"))
    icon = (b.app / "Assets.car").exists() and bool(i.get("CFBundleIcons") or i.get("CFBundleIconName"))
    r.append(Result("Info.plist: app icon", icon, i.get("CFBundleIconName") or
                    ("Assets.car present" if (b.app / "Assets.car").exists() else "no Assets.car")))
    r.append(Result("Info.plist: launch screen", "UILaunchScreen" in i or "UILaunchStoryboardName" in i,
                    "UILaunchScreen" if "UILaunchScreen" in i else i.get("UILaunchStoryboardName", "missing")))
    enc = i.get("ITSAppUsesNonExemptEncryption")
    r.append(Result("Info.plist: ITSAppUsesNonExemptEncryption", isinstance(enc, bool),
                    "missing" if enc is None else str(enc)))
    r.append(Result("Info.plist: NSLocalNetworkUsageDescription", bool(i.get("NSLocalNetworkUsageDescription")),
                    "present" if i.get("NSLocalNetworkUsageDescription") else "missing"))
    bg = i.get("BGTaskSchedulerPermittedIdentifiers")
    r.append(Result("Info.plist: BGTaskSchedulerPermittedIdentifiers", bool(bg), ", ".join(bg or []) or "missing"))
    r.append(Result("Info.plist: no UIFileSharingEnabled", "UIFileSharingEnabled" not in i,
                    "UIFileSharingEnabled is set" if "UIFileSharingEnabled" in i else ""))
    return r


def _framework_binary(b: Bundle, f: Path) -> bool:
    rel = f.relative_to(b.app).parts
    return len(rel) == 3 and rel[0] == "Frameworks" and rel[1] == rel[2] + ".framework"


def check_structure(b: Bundle) -> list[Result]:
    r = []
    stray = [str(f.relative_to(b.app)) for f in b.machos if f != b.main and not _framework_binary(b, f)]
    fws = sorted((b.app / "Frameworks").glob("*.framework")) if (b.app / "Frameworks").is_dir() else []
    missing = [fw.name for fw in fws if not (fw / fw.stem).is_file() or (fw / fw.stem) not in b.machos]
    extra = []
    for fw in fws:
        n = sum(1 for f in b.machos if fw in f.parents)
        if n > 1:
            extra.append(f"{fw.name} holds {n} binaries")
    r.append(_check("bundle: Mach-O files only as the executable and one per framework", stray + missing + extra,
                    f"{len(b.machos)} binaries, {len(fws)} frameworks"))
    bad = [str(f.relative_to(b.app)) for f in b.files if f.suffix in (".so", ".dylib", ".a", ".o")]
    r.append(_check("bundle: no .so, .dylib, .a or .o files", bad))
    probs, ids = [], {}
    for fw in fws:
        p = fw / "Info.plist"
        try:
            d = plistlib.loads(p.read_bytes())
        except Exception:  # noqa: BLE001
            probs.append(f"{fw.name}: no readable Info.plist")
            continue
        if d.get("CFBundlePackageType") != "FMWK":
            probs.append(f"{fw.name}: CFBundlePackageType {d.get('CFBundlePackageType')!r}")
        if d.get("CFBundleExecutable") != fw.stem:
            probs.append(f"{fw.name}: CFBundleExecutable {d.get('CFBundleExecutable')!r}")
        fid = d.get("CFBundleIdentifier", "")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", fid or ""):
            probs.append(f"{fw.name}: CFBundleIdentifier {fid!r}")
        elif fid in ids:
            probs.append(f"{fw.name}: CFBundleIdentifier {fid} also used by {ids[fid]}")
        ids.setdefault(fid, fw.name)
        if not d.get("MinimumOSVersion"):
            probs.append(f"{fw.name}: no MinimumOSVersion")
        if not d.get("CFBundleSupportedPlatforms"):
            probs.append(f"{fw.name}: no CFBundleSupportedPlatforms")
    r.append(_check("frameworks: Info.plist (FMWK, executable, identifier, MinimumOSVersion, platforms)", probs))
    probs = []
    for f in b.files:
        if f.suffix == ".fwork":
            t = b.app / f.read_text().strip()
            if not t.is_file() or not _framework_binary(b, t):
                probs.append(f"{f.relative_to(b.app)} -> {f.read_text().strip()} (missing)")
        elif f.suffix == ".origin" and "Frameworks" in f.relative_to(b.app).parts[:1]:
            t = b.app / f.read_text().strip()
            if not t.is_file():
                probs.append(f"{f.relative_to(b.app)} -> {f.read_text().strip()} (missing)")
    nfw = sum(1 for f in b.files if f.suffix == ".fwork")
    r.append(_check("frameworks: every .fwork and .origin points at a file that exists", probs, f"{nfw} .fwork files"))
    probs = []
    for f in b.files:
        rel = str(f.relative_to(b.app))
        if f.name == "pocketnode_token":
            probs.append(rel)
        elif f.name.startswith("."):
            probs.append(rel)
        elif f.name == "__pycache__" or "__pycache__" in f.parts[len(b.app.parts):]:
            probs.append(rel)
    for root, dirs, _ in os.walk(b.app):
        for d in dirs:
            if d == "__pycache__" or d.startswith("."):
                probs.append(str((Path(root) / d).relative_to(b.app)) + "/")
    r.append(_check("bundle: no token, stamps, dotfiles, .DS_Store or __pycache__", sorted(set(probs))))
    probs = []
    real = b.app.resolve()
    for f in b.files:
        if f.is_symlink():
            t = (f.parent / os.readlink(f)).resolve()
            if real not in t.parents and t != real:
                probs.append(f"{f.relative_to(b.app)} -> {os.readlink(f)}")
    r.append(_check("bundle: no symlinks pointing outside it", probs))
    return r


def _sdk_lib_exists(lib: str) -> bool:
    from . import symcheck
    return symcheck.exports_of(lib) is not None


def check_binaries(b: Bundle) -> list[Result]:
    r = []
    rel = {f: str(f.relative_to(b.app)) for f in b.machos}
    if b.sdk == "iphoneos":
        probs = [f"{rel[f]}: {', '.join(s.arch for s in sl)}" for f, sl in b.machos.items()
                 if [s.arch for s in sl] != ["arm64"]]
        r.append(_check("binaries: arm64 only", probs, f"{len(b.machos)} binaries"))
        probs = [f"{rel[f]}: {PLATFORMS.get(s.platform, s.platform)}" for f, sl in b.machos.items()
                 for s in sl[:1] if s.platform != 2]
        r.append(_check("binaries: LC_BUILD_VERSION platform iOS", probs))
        probs = [rel[f] for f, sl in b.machos.items() if not all(s.encryption for s in sl)]
        r.append(_check("binaries: LC_ENCRYPTION_INFO_64 present", probs))
    else:
        # A simulator build is never uploaded: its Python.framework is fat (arm64 and
        # x86_64), its binaries are iOS Simulator builds and the linker adds no encryption info.
        fat = sum(1 for sl in b.machos.values() if len(sl) > 1)
        sim = sum(1 for sl in b.machos.values() if sl[0].platform == 7)
        why = "device builds only; this is a simulator build"
        r.append(Result("binaries: arm64 only", None, f"{why} ({fat} of {len(b.machos)} binaries are fat)"))
        r.append(Result("binaries: LC_BUILD_VERSION platform iOS", None,
                        f"{why} ({sim} of {len(b.machos)} binaries are iOS Simulator builds)"))
        r.append(Result("binaries: LC_ENCRYPTION_INFO_64 present", None, why))
    app_min = _ver(b.info.get("MinimumOSVersion"))
    probs = [f"{rel[f]}: minos {s.minos}" for f, sl in b.machos.items() for s in sl[:1]
             if _ver(s.minos) > app_min]
    r.append(_check("binaries: minos not above the app's MinimumOSVersion", probs))
    kinds: dict[str, int] = {}
    probs = []
    for f, sl in b.machos.items():
        ft = sl[0].filetype
        if f == b.main:
            if ft != MH_EXECUTE:
                probs.append(f"{rel[f]}: {FILETYPES.get(ft, ft)} (expected executable)")
            continue
        ext = any(x.suffix == ".origin" for x in f.parent.iterdir()) if _framework_binary(b, f) else False
        kinds[f"{'extension module' if ext else 'library'} {FILETYPES.get(ft, ft)}"] = \
            kinds.get(f"{'extension module' if ext else 'library'} {FILETYPES.get(ft, ft)}", 0) + 1
        if ft != MH_DYLIB and not (ext and ft == MH_BUNDLE):
            probs.append(f"{rel[f]}: {FILETYPES.get(ft, ft)}")
    r.append(_check("binaries: frameworks are dylibs (extension modules: dylib or bundle)", probs,
                    ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))))
    bundled = {f"@rpath/{f.parent.name}/{f.name}" for f in b.machos if _framework_binary(b, f)}
    probs, private = [], []
    for f, sl in b.machos.items():
        for lib, _kind in sl[0].deps:
            if "/PrivateFrameworks/" in lib:
                private.append(f"{rel[f]}: {lib}")
            if lib.startswith("@rpath/"):
                if lib not in bundled:
                    probs.append(f"{rel[f]}: {lib} (not in the bundle)")
            elif lib.startswith("@"):
                probs.append(f"{rel[f]}: {lib}")
            elif not _sdk_lib_exists(lib):
                probs.append(f"{rel[f]}: {lib} (not in the iOS SDK)")
    r.append(_check("binaries: every dependent library is in the iOS SDK or bundled", probs))
    r.append(_check("binaries: no private frameworks linked", private))
    return r


def _leafs(lib: str) -> set[str]:
    base = os.path.basename(lib)
    return {base, base.split(".")[0], os.path.splitext(base)[0]}


def check_symbols(b: Bundle) -> list[Result]:
    from . import symcheck
    rel = {f: str(f.relative_to(b.app)) for f in b.machos}
    by_install = {f"@rpath/{f.parent.name}/{f.name}": f for f in b.machos if _framework_binary(b, f)}
    py = b.app / "Frameworks" / "Python.framework" / "Python"
    cache: dict[Path, set[str]] = {}

    def exp(f: Path) -> set[str]:
        if f not in cache:
            cache[f] = exports(f)
        return cache[f]

    probs = []
    for f, sl in b.machos.items():
        leafmap: dict[str, str] = {}
        for lib, _k in sl[0].deps:
            for lf in _leafs(lib):
                leafmap.setdefault(lf, lib)
        for sym, weak, src in b.imports.get(f, []):
            if weak:
                continue
            if src in ("<flat-namespace>", "flat-namespace"):
                ok = (py in b.machos and sym in exp(py)) or sym in (symcheck.exports_of("/usr/lib/libSystem.B.dylib") or ())
                if not ok:
                    probs.append(f"{rel[f]}: {sym} (flat lookup)")
                continue
            if src in ("this-image", "main-executable", "<this-image>", "<main-executable>"):
                continue
            lib = leafmap.get(src)
            if lib is None:
                continue  # dyld_info printed a name we can't map; the library check covers presence
            if lib in by_install:
                if sym not in exp(by_install[lib]):
                    probs.append(f"{rel[f]}: {sym} (not exported by {by_install[lib].name})")
            elif not lib.startswith("@"):
                ex = symcheck.exports_of(lib)
                if ex is not None and sym not in ex:
                    probs.append(f"{rel[f]}: {sym} (not in the iOS SDK's {os.path.basename(lib)})")
    return [_check("symbols: every import resolves (iOS SDK stubs, bundled frameworks, Python)", probs,
                   f"{sum(len(v) for v in b.imports.values())} imports")]


def check_leftovers(b: Bundle) -> list[Result]:
    r = []
    fws = sorted((b.app / "Frameworks").glob("*.framework")) if (b.app / "Frameworks").is_dir() else []
    bad = [fw.name for fw in fws if fw.name.startswith(("pocketnode_shim_", "pocketnode_absent_", "libtorch"))]
    bad += [str(f.relative_to(b.app)) for f in b.files if f.name.startswith("libtorch")]
    r.append(_check("leftovers: no shim or stand-in frameworks, no libtorch", sorted(set(bad))))
    # In Debug builds Xcode moves the app's code into <name>.debug.dylib; it counts as the executable.
    mains = [f for f in (b.main, b.main.with_name(b.main.name + ".debug.dylib")) if f in b.machos]
    hits = sorted({f"{f.name}: {s}" for f in mains for s, _w, _src in b.imports.get(f, []) if s in HOSTILE_MAIN})
    r.append(_check("leftovers: executable doesn't add or replace Objective-C methods", hits))
    hits = sorted({f.name for f in mains for s, _w, _src in b.imports.get(f, []) if s == "__PyRuntime"})
    r.append(_check("leftovers: executable doesn't reach into _PyRuntime", hits))
    hits = []
    for f in b.files:
        if f.is_file() and not f.is_symlink():
            try:
                if b"itms-services" in f.read_bytes():
                    hits.append(str(f.relative_to(b.app)))
            except OSError:
                pass
    r.append(_check('leftovers: the string "itms-services" appears nowhere', hits))
    s = b.app / "pocketnode_settings.json"
    probs = []
    try:
        import json
        d = json.loads(s.read_text())
        if d.get("edition") != "store":
            probs.append(f"edition is {d.get('edition')!r}")
        if b.archive and d.get("debug") is not False:
            probs.append(f"debug is {d.get('debug')!r} in an archive")
        if any("token" in k for k in d):
            probs.append("has a token")
        detail = f"edition {d.get('edition')}, debug {d.get('debug')}, tls {d.get('tls')}"
    except (OSError, ValueError) as e:
        probs.append(f"unreadable ({e})")
        detail = ""
    r.append(_check("leftovers: pocketnode_settings.json is the store edition's" +
                    (" (debug off)" if b.archive else ""), probs, detail))
    return r


def required_categories(b: Bundle, f: Path) -> dict[str, str]:
    out = {}
    for sym, _w, _src in b.imports.get(f, []):
        if sym in REASON_SYMBOLS:
            cat, reason = REASON_SYMBOLS[sym]
            out[cat] = reason
    return out


def check_privacy(b: Bundle) -> list[Result]:
    r = []
    root = b.app / "PrivacyInfo.xcprivacy"
    cats = manifest_categories(root) if root.exists() else None
    probs = []
    if cats is None:
        probs.append("missing or unreadable" if root.exists() else "missing")
    else:
        d = plistlib.loads(root.read_bytes())
        if d.get("NSPrivacyTracking") is not False:
            probs.append("NSPrivacyTracking isn't false")
        if d.get("NSPrivacyTrackingDomains"):
            probs.append("lists tracking domains")
        for cat, reason in CATEGORIES.items():
            if reason not in cats.get(cat, set()):
                probs.append(f"no {cat} with {reason}")
    r.append(_check("privacy: app manifest (no tracking, file timestamp, boot time, disk space)", probs,
                    "PrivacyInfo.xcprivacy"))
    probs, n = [], 0
    for f in b.machos:
        if not _framework_binary(b, f):
            continue
        need = required_categories(b, f)
        if not need:
            continue
        n += 1
        m = f.parent / "PrivacyInfo.xcprivacy"
        have = manifest_categories(m) if m.exists() else None
        if have is None:
            probs.append(f"{f.parent.name}: no manifest (uses {', '.join(sorted(need))})")
            continue
        lacking = [c for c, reason in need.items() if reason not in have.get(c, set())]
        if lacking:
            probs.append(f"{f.parent.name}: doesn't declare {', '.join(lacking)}")
    r.append(_check("privacy: a manifest in every framework that uses a required-reason API", probs,
                    f"{n} frameworks need one"))
    return r


def check_sizes(b: Bundle) -> list[Result]:
    total = sum(f.stat().st_size for f in b.files if f.is_file() and not f.is_symlink())
    text = sum(sl[0].text for sl in b.machos.values())
    big = max(b.machos.items(), key=lambda kv: kv[1][0].text, default=(None, None))
    detail = f"app {total / 2**20:.0f} MB, __TEXT {text / 2**20:.1f} MB in total"
    if big[0] is not None:
        detail += f" (largest {big[0].name} {big[1][0].text / 2**20:.1f} MB)"
    return [Result("sizes: __TEXT under 500 MB", text < 500 * 2**20, detail)]


def list_selectors(b: Bundle) -> list[Result]:
    out = []
    for f, sl in sorted(b.machos.items()):
        if f == b.main or f.name == b.main.name + ".debug.dylib":
            continue
        if sl[0].methnames:
            out.append(Result(f"objc selectors in {f.relative_to(b.app)}", True,
                              ", ".join(sl[0].methnames), info=True))
    if not out:
        out.append(Result("objc selectors outside the executable", True, "none", info=True))
    return out


def validate(path: Path) -> tuple[Bundle, list[Result]]:
    from . import symcheck
    symcheck.use_sdk("iphoneos")  # the device SDK's stubs (the simulator's libSystem stubs give false misses)
    b = load(path)
    results = []
    for fn in (check_info_plist, check_structure, check_binaries, check_symbols, check_leftovers,
               check_privacy, check_sizes, list_selectors):
        results += fn(b)
    return b, results


def default_path(sdk: str | None = None) -> Path | None:
    """The last store archive, else the newest store build. With `sdk`, the store build for that SDK."""
    from . import app
    if sdk:
        p = app.product(sdk, "store")
        return p if p.exists() else None
    arc = app.archive_path()
    if arc.exists():
        return arc
    builds = [p for p in (app.product(s, "store") for s in ("iphoneos", "iphonesimulator")) if p.exists()]
    return max(builds, key=lambda p: p.stat().st_mtime) if builds else None


def main(path: str | Path | None = None, out=print, sdk: str | None = None) -> int:
    p = Path(path) if path else default_path(sdk)
    if p is None:
        out("nothing to validate: build the store edition first (`pocketnode app archive` or "
            "`pocketnode app build --store`), or give a path to an .xcarchive or .app")
        return 1
    try:
        b, results = validate(p)
    except (FileNotFoundError, plistlib.InvalidFileException) as e:
        out(str(e))
        return 1
    out(f"{b.app} ({'archive' if b.archive else 'app'}, {b.sdk})")
    checks = [x for x in results if not x.info]
    width = max(len(x.name) for x in checks)
    label = {True: "PASS", False: "FAIL", None: "SKIP"}
    for x in checks:
        out(f"{label[x.ok]}  {x.name:<{width}}  {x.detail}")
    info = [x for x in results if x.info]
    if info:
        out("\nObjective-C selectors referenced outside the executable (for a person to look at):")
        for x in info:
            out(f"  {x.name.removeprefix('objc selectors in ')}: {x.detail}")
    p_, f_, s_ = (sum(x.ok is v for x in checks) for v in (True, False, None))
    out(f"\n{p_} passed, {f_} failed, {s_} skipped")
    return 1 if f_ else 0
