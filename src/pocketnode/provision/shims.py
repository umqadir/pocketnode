"""Shim libraries for symbols macOS binaries import but iOS lacks.

A shim re-exports the real library and adds the few missing symbols, and the
binary's reference to the library is pointed at the shim. Two sources of
implementations, both behaving the way the API itself defines failure:

- a hand-written table for symbols with a correct iOS meaning;
- any function the macOS SDK declares as returning OSStatus gets a stub
  returning errSecUnimplemented (-4): the API's own "not supported here".

Anything else leaves the module out, with the reason reported.
"""

from __future__ import annotations

import functools
import hashlib
import re
import subprocess
from pathlib import Path

# symbol (Mach-O name) -> C definition
IMPLEMENTATIONS = {
    # libgcc's trampoline helper toggles W^X for MAP_JIT pages; iOS apps have none.
    "_pthread_jit_write_protect_np": "void pthread_jit_write_protect_np(int enabled) { (void)enabled; }",
    # BLAS helper macOS Accelerate exports and iOS Accelerate doesn't: |Re z| + |Im z|.
    "_dcabs1_": "#include <math.h>\ndouble dcabs1_(const double *z) { return fabs(z[0]) + fabs(z[1]); }",
    # macOS GPU hot-plug API (MLX). An iPhone has exactly one GPU and nothing to observe.
    "_MTLCopyAllDevicesWithObserver": (
        "#import <Metal/Metal.h>\n"
        "__attribute__((ns_returns_retained)) NSArray *MTLCopyAllDevicesWithObserver("
        "id __strong *observer, id handler) {\n"
        "  if (observer) *observer = [NSObject new];\n"
        "  id<MTLDevice> d = MTLCreateSystemDefaultDevice();\n"
        "  return d ? @[d] : @[];\n}"),
    "_MTLRemoveDeviceObserver": "void MTLRemoveDeviceObserver(id observer) { (void)observer; }",
}


@functools.lru_cache(None)
def _macos_headers(lib: str) -> str:
    """Concatenated macOS SDK headers for a framework (empty if not a framework)."""
    m = re.search(r"/([^/]+)\.framework/", lib)
    if not m:
        return ""
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    root = Path(sdk) / "System/Library/Frameworks" / f"{m.group(1)}.framework"
    return "\n".join(p.read_text(errors="replace") for p in root.rglob("*.h"))


def implementation(sym: str, lib: str) -> str | None:
    if sym in IMPLEMENTATIONS:
        return IMPLEMENTATIONS[sym]
    name = sym[1:] if sym.startswith("_") else sym
    if re.search(rf"\bOSStatus\s+{re.escape(name)}\s*\(", _macos_headers(lib)):
        return f"int {name}() {{ return -4; /* errSecUnimplemented */ }}"
    return None


def can_shim(symbols, lib: str = "") -> bool:
    return all(implementation(s, lib) is not None for s in symbols)


def _reexport_flag(lib: str) -> list[str]:
    if ".framework/" in lib:
        name = lib.split("/")[-1]
        return [f"-Wl,-reexport_framework,{name}"]
    leaf = Path(lib).name
    if leaf.startswith("lib") and ".dylib" in leaf:
        return [f"-Wl,-reexport-l{leaf[3:].split('.')[0]}"]
    raise ValueError(f"don't know how to re-export {lib}")


def build(lib: str, symbols: list[str], sdk: str, minos: str, out_dir: Path) -> str:
    """Build (or reuse) a shim framework for `lib` + `symbols`. Returns its framework name."""
    syms = sorted(set(symbols))
    tag = hashlib.sha1(f"{lib}|{'|'.join(syms)}".encode()).hexdigest()[:8]
    name = f"pocketnode_shim_{Path(lib).name.split('.')[0]}_{tag}"
    fw = out_dir / f"{name}.framework"
    binary = fw / name
    if binary.exists():
        return name
    fw.mkdir(parents=True, exist_ok=True)
    src = fw / "shim.m"
    src.write_text("\n".join(implementation(s, lib) for s in syms) + "\n")
    target = f"arm64-apple-ios{minos}" + ("-simulator" if sdk == "iphonesimulator" else "")
    sdkpath = subprocess.check_output(["xcrun", "--sdk", sdk, "--show-sdk-path"], text=True).strip()
    subprocess.run(["xcrun", "--sdk", sdk, "clang", "-target", target, "-isysroot", sdkpath, "-dynamiclib",
                    "-fobjc-arc", "-O2", "-w", "-framework", "Foundation",
                    "-install_name", f"@rpath/{name}.framework/{name}", *_reexport_flag(lib),
                    str(src), "-o", str(binary)], check=True, capture_output=True)
    src.unlink()
    return name


def build_absent(lib: str, symbols: list[str], sdk: str, minos: str, out_dir: Path) -> str:
    """A stand-in for a framework iOS doesn't have, defining exactly what's imported.

    Data symbols are null, functions return 0, Objective-C classes are empty
    NSObject subclasses. The binary loads; only the features that use the
    framework (GUI, clipboard, ...) don't work, as on any system without it.
    """
    syms = sorted(set(symbols))
    leaf = Path(lib).name.split(".")[0]
    tag = hashlib.sha1(f"absent|{lib}|{'|'.join(syms)}".encode()).hexdigest()[:8]
    name = f"pocketnode_absent_{leaf}_{tag}"
    fw = out_dir / f"{name}.framework"
    binary = fw / name
    if binary.exists():
        return name
    fw.mkdir(parents=True, exist_ok=True)
    headers = _macos_headers(lib)
    lines = ["#import <Foundation/Foundation.h>"]
    for s in syms:
        n = s[1:] if s.startswith("_") else s
        if n.startswith("OBJC_CLASS_$_"):
            cls = n[len("OBJC_CLASS_$_"):]
            lines.append(f"@interface {cls} : NSObject @end\n@implementation {cls} @end")
        elif n.startswith("OBJC_METACLASS_$_"):
            continue
        elif re.search(rf"\b{re.escape(n)}\s*\(", headers):
            lines.append(f"long {n}(void) {{ return 0; }}")
        else:
            lines.append(f"void *{n} = 0;")
    src = fw / "absent.m"
    src.write_text("\n".join(lines) + "\n")
    target = f"arm64-apple-ios{minos}" + ("-simulator" if sdk == "iphonesimulator" else "")
    sdkpath = subprocess.check_output(["xcrun", "--sdk", sdk, "--show-sdk-path"], text=True).strip()
    subprocess.run(["xcrun", "--sdk", sdk, "clang", "-target", target, "-isysroot", sdkpath, "-dynamiclib",
                    "-fobjc-arc", "-O2", "-w", "-framework", "Foundation",
                    "-install_name", f"@rpath/{name}.framework/{name}", str(src), "-o", str(binary)],
                   check=True, capture_output=True)
    src.unlink()
    return name


def weak_link(p: Path, libs: set[str]) -> int:
    """Turn LC_LOAD_DYLIB of `libs` into LC_LOAD_WEAK_DYLIB (same layout, different cmd).

    A weak-linked library that's missing at load time doesn't stop the binary
    loading; only the features that call into it are unavailable.
    """
    import struct
    data = bytearray(p.read_bytes())
    ncmds = struct.unpack_from("<I", data, 16)[0]
    off, n = 32, 0
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, off)
        if cmd == 0xC:
            name_off = struct.unpack_from("<I", data, off + 8)[0]
            name = bytes(data[off + name_off: off + size]).split(b"\0")[0].decode()
            if name in libs:
                struct.pack_into("<I", data, off, 0x80000018)
                n += 1
        off += size
    if n:
        p.write_bytes(data)
    return n
