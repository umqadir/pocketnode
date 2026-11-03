"""Shim libraries for symbols macOS binaries import but iOS lacks.

A shim re-exports the real library and adds the few missing symbols, and the
binary's reference to the library is pointed at the shim. Only symbols with a
correct iOS implementation are listed; anything else leaves the module out.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

# symbol (Mach-O name) -> C definition
IMPLEMENTATIONS = {
    # libgcc's trampoline helper toggles W^X for MAP_JIT pages; iOS apps have none.
    "_pthread_jit_write_protect_np": "void pthread_jit_write_protect_np(int enabled) { (void)enabled; }",
    # BLAS helper macOS Accelerate exports and iOS Accelerate doesn't: |Re z| + |Im z|.
    "_dcabs1_": "#include <math.h>\ndouble dcabs1_(const double *z) { return fabs(z[0]) + fabs(z[1]); }",
}


def can_shim(symbols) -> bool:
    return all(s in IMPLEMENTATIONS for s in symbols)


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
    src = fw / "shim.c"
    src.write_text("\n".join(IMPLEMENTATIONS[s] for s in syms) + "\n")
    target = f"arm64-apple-ios{minos}" + ("-simulator" if sdk == "iphonesimulator" else "")
    sdkpath = subprocess.check_output(["xcrun", "--sdk", sdk, "--show-sdk-path"], text=True).strip()
    subprocess.run(["xcrun", "--sdk", sdk, "clang", "-target", target, "-isysroot", sdkpath, "-dynamiclib",
                    "-O2", "-install_name", f"@rpath/{name}.framework/{name}", *_reexport_flag(lib),
                    str(src), "-o", str(binary)], check=True, capture_output=True)
    src.unlink()
    return name
