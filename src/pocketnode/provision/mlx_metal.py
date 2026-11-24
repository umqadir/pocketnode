"""An iOS build of MLX's GPU kernels, matching the Mac's MLX version exactly.

MLX's compiled C++ retags like any other package; its Metal kernels ship as a
macOS metallib, which the iOS GPU can't use. This compiles the same kernel
sources (from the matching MLX git tag) with the iOS Metal toolchain, NAX
kernels included, and links them into an mlx.metallib that MLX loads from
next to libmlx.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import progress as progress_mod
from ..sources import CONFIG_DIR

CACHE = CONFIG_DIR / "cache"
METAL_MIN = "26.2"  # Metal 4 tensor ops (NAX) need iOS 26.2+


def source(version: str, progress=None) -> Path:
    src = CACHE / "src" / f"mlx-{version}"
    if not (src / "mlx").exists():
        (progress or progress_mod.NULL).detail(f"downloading MLX {version} sources")
        src.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "-b", f"v{version}",
                        "https://github.com/ml-explore/mlx.git", str(src)], check=True)
    return src


def kernels(src: Path) -> list[str]:
    cm = (src / "mlx/backend/metal/kernels/CMakeLists.txt").read_text()
    return sorted(set(re.findall(r"build_kernel\(\s*([A-Za-z0-9_/]+)", cm)) - {"KERNEL"})


def build(version: str, sdk: str, progress=None) -> Path:
    """mlx.metallib for `sdk` ("iphoneos" or "iphonesimulator"), cached per version.
    progress: a reporter told `compiling GPU shaders k/n` (a cached library reports nothing)."""
    out = CACHE / "mlx-metallib" / f"{version}-{sdk}" / "mlx.metallib"
    if out.exists():
        return out
    p = progress or progress_mod.NULL
    src = source(version, p)
    kdir = src / "mlx/backend/metal/kernels"
    work = out.parent / "air"
    work.mkdir(parents=True, exist_ok=True)
    minflag = (f"-mios-simulator-version-min={METAL_MIN}" if sdk == "iphonesimulator"
               else f"-mios-version-min={METAL_MIN}")

    names = kernels(src)
    tick = progress_mod.counter(p, len(names), "compiling MLX GPU shaders")

    def compile_one(k: str) -> Path:
        try:
            return _compile(k)
        finally:
            tick()

    def _compile(k: str) -> Path:
        air = work / (k.replace("/", "_") + ".air")
        r = subprocess.run(["xcrun", "-sdk", sdk, "metal", "-x", "metal", "-Wall", "-Wextra", "-fno-fast-math",
                            "-Wno-c++17-extensions", "-Wno-c++20-extensions", minflag, "-c",
                            str(kdir / f"{k}.metal"), f"-I{src}", "-o", str(air)], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"metal compile of {k} failed:\n{r.stderr[-2000:]}")
        return air

    with ThreadPoolExecutor(8) as pool:
        airs = list(pool.map(compile_one, names))
    p.detail("linking MLX GPU shaders")
    subprocess.run(["xcrun", "-sdk", sdk, "metallib", *map(str, airs), "-o", str(out)], check=True,
                   capture_output=True)
    shutil.rmtree(work, ignore_errors=True)
    return out


def install(stage_root: Path, version: str, sdk: str, progress=None) -> Path | None:
    """Swap the macOS metallib in a staged app for the iOS one."""
    fw = next((stage_root / "Frameworks").glob("mlx.lib.libmlx.framework"), None)
    if fw is None:
        return None
    lib = build(version, sdk, progress)
    shutil.copy2(lib, fw / "mlx.metallib")
    mac = stage_root / "app_packages" / "mlx" / "lib" / "mlx.metallib"
    if mac.exists():
        mac.unlink()
    return fw / "mlx.metallib"
