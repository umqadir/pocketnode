"""An iOS build of PyTorch's own Metal shaders, matching the Mac's torch version.

torch's MPS backend mostly runs on Apple's MPSGraph, which works on the iPhone
GPU as is. The rest of its operations use torch's own shaders, which the wheel
embeds as a macOS metallib in the `__TEXT,metal_basic` section of
libtorch_cpu.dylib; torch reads that section from the loaded image whose path
contains "/libtorch_cpu.dylib". This compiles the same shaders (from the
matching PyTorch git tag) with the iOS Metal toolchain and writes them into
that section in place.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import progress as progress_mod
from ..sources import CONFIG_DIR

CACHE = CONFIG_DIR / "cache"
# The library has to fit in the wheel's section, which can't grow (other __TEXT
# sections follow it). Current Metal toolchains emit about 1% more bitcode
# than the one torch's wheels are built with, so it's compressed, which needs
# the MetalLib format of iOS 26.
METAL_MIN = "26.0"
_SPARSE = ("aten/src/ATen/native/mps", "aten/src/ATen/native/sparse/mps", "c10/metal")


def _base(version: str) -> str:
    return version.split("+")[0]


def source(version: str, progress=None) -> Path:
    version = _base(version)
    src = CACHE / "src" / f"pytorch-{version}"
    if not (src / "aten/src/ATen/native/mps/kernels").exists():
        (progress or progress_mod.NULL).detail(f"downloading PyTorch {version} shader sources")
        shutil.rmtree(src, ignore_errors=True)  # an interrupted clone
        src.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-c", "advice.detachedHead=false", "clone", "-q", "--filter=blob:none", "--sparse", "--depth", "1",
                        "-b", f"v{version}", "https://github.com/pytorch/pytorch.git", str(src)], check=True)
        subprocess.run(["git", "-C", str(src), "sparse-checkout", "set", *_SPARSE], check=True)
    return src


def build(version: str, sdk: str, progress=None) -> Path:
    """kernels_basic.metallib for `sdk` ("iphoneos" or "iphonesimulator"), cached per version.

    Compiled as torch's own build does (cmake/Metal.cmake, aten/src/ATen/CMakeLists.txt).
    progress: a reporter told `compiling GPU shaders k/n` (a cached library reports nothing).
    """
    version = _base(version)
    out = CACHE / "torch-metallib" / f"{version}-{sdk}" / "kernels_basic.metallib"
    if out.exists():
        return out
    p = progress or progress_mod.NULL
    src = source(version, p)
    native = src / "aten/src/ATen/native"
    shaders = sorted([*(native / "mps").rglob("*.metal"), *(native / "sparse/mps").rglob("*.metal")])
    work = out.parent / "air"
    work.mkdir(parents=True, exist_ok=True)
    minflag = (f"-mios-simulator-version-min={METAL_MIN}" if sdk == "iphonesimulator"
               else f"-mios-version-min={METAL_MIN}")

    tick = progress_mod.counter(p, len(shaders), "compiling PyTorch GPU shaders")

    def compile_one(s: Path) -> Path:
        try:
            return _compile(s)
        finally:
            tick()

    def _compile(s: Path) -> Path:
        # Named by path, since torch's own build names .air files by stem alone.
        air = work / (str(s.relative_to(native).with_suffix("")).replace("/", "_") + ".air")
        r = subprocess.run(["xcrun", "-sdk", sdk, "metal", "-c", str(s), "-I", str(src), "-I", str(src / "aten/src"),
                            "-o", str(air), "-std=metal3.1", "-Wall", "-Wextra", "-fno-fast-math", minflag],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"metal compile of {s.relative_to(src)} failed:\n{r.stderr[-2000:]}")
        return air

    with ThreadPoolExecutor(8) as pool:
        airs = list(pool.map(compile_one, shaders))
    p.detail("linking PyTorch GPU shaders")
    linked, tmp = work / "linked.metallib", out.with_suffix(".tmp")
    for cmd in (["metallib", "-o", str(linked), *map(str, airs)],
                ["metal-strip", "--compress-sections=MODULE_LIST", "--reflection_metadata", f"-o={tmp}", str(linked)]):
        r = subprocess.run(["xcrun", "-sdk", sdk, *cmd], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"{cmd[0]} failed:\n{r.stderr[-2000:]}")
    tmp.rename(out)
    shutil.rmtree(work, ignore_errors=True)
    return out


def patch(binary: Path, metallib: Path) -> None:
    """Replace the contents of `__TEXT,metal_basic` in `binary` (in place, thinned to arm64).

    The section's size becomes the metallib's length, since torch hands the
    whole section to Metal. Leaves the code signature invalid; the caller
    re-signs.
    """
    with open(binary, "rb") as f:
        magic = f.read(4)
    if magic != b"\xcf\xfa\xed\xfe":
        subprocess.run(["lipo", "-thin", "arm64", str(binary), "-output", str(binary)],
                       check=True, capture_output=True)
    lib = metallib.read_bytes()
    with open(binary, "r+b") as f:
        head = f.read(32)
        ncmds, sizeofcmds = struct.unpack_from("<II", head, 16)
        cmds = f.read(sizeofcmds)
        off = 0
        for _ in range(ncmds):
            cmd, size = struct.unpack_from("<II", cmds, off)
            if cmd == 0x19:  # LC_SEGMENT_64
                nsects = struct.unpack_from("<I", cmds, off + 64)[0]
                for i in range(nsects):
                    sec = off + 72 + 80 * i
                    if cmds[sec: sec + 16].rstrip(b"\0") == b"metal_basic" and \
                            cmds[sec + 16: sec + 32].rstrip(b"\0") == b"__TEXT":
                        cur, fileoff = struct.unpack_from("<QI", cmds, sec + 40)
                        if len(lib) > cur:
                            raise ValueError(f"metallib is {len(lib)} bytes; {binary.name}'s "
                                             f"__TEXT,metal_basic section holds only {cur}")
                        f.seek(fileoff)
                        f.write(lib + b"\0" * (cur - len(lib)))
                        f.seek(32 + sec + 40)
                        f.write(struct.pack("<Q", len(lib)))
                        return
            off += size
    raise LookupError(f"{binary} has no __TEXT,metal_basic section")


def install(pk: Path, version: str, sdk: str, progress=None) -> bool:
    """Put iOS shaders into a staged torch's libtorch_cpu.dylib."""
    lib = pk / "torch" / "lib" / "libtorch_cpu.dylib"
    if not lib.exists():
        return False
    patch(lib, build(version, sdk, progress))
    return True
