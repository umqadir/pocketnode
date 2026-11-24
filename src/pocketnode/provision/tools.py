"""Build tools and downloads the iOS app needs, fetched into the pocketnode cache.

    xcodegen()            the XcodeGen binary (from PATH, else a pinned release)
    python_xcframework()  BeeWare's embedded CPython for iOS (pinned release)
    metal_toolchain()     whether Xcode's Metal toolchain is installed (GPU shaders)

Downloads are pinned to one release each and checked against its SHA-256.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .. import progress as progress_mod
from ..sources import CONFIG_DIR

CACHE = CONFIG_DIR / "cache"

XCODEGEN = {
    "version": "2.48.0",
    "url": "https://github.com/yonaskolb/XcodeGen/releases/download/2.48.0/xcodegen.zip",
    "sha256": "620c9f0dc10849f94728ab5df52d225ebab8187439dd6fee1925e59e80dff892",
    "bytes": 5738697,
}
# The phone's Python. Its minor version must match the Mac's (bytecode and
# extension modules are shared), so this moves to a new minor only together
# with requires-python.
PYTHON_SUPPORT = {
    "python": "3.13",
    "build": "b18",
    "url": "https://github.com/beeware/Python-Apple-support/releases/download/3.13-b18/"
           "Python-3.13-iOS-support.b18.tar.gz",
    "sha256": "d06acf2a9c7964a8689566353a0e707402eff822037de5e29ba0e16040f7047d",
    "bytes": 34524958,
    "mb": 35,
}
METAL_TOOLCHAIN_MB = 840  # measured with Xcode 27.1


class ToolError(RuntimeError):
    pass


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, sha256: str, size: int | None = None, progress=None) -> Path:
    """Fetch `url` to `dest` unless a file with the right checksum is already there.
    size: the expected bytes (for the count); progress: a reporter told the MB done/total."""
    if dest.exists() and _sha256(dest) == sha256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    p = progress or progress_mod.NULL
    total = round(size / 2**20, 1) if size else None
    # curl uses the system's certificates, which a standalone Python build may not find.
    proc = subprocess.Popen(["curl", "-fL", "--retry", "3", "--silent", "--show-error", "-o", str(part), url],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    while proc.poll() is None:
        try:
            got = part.stat().st_size
        except OSError:
            got = 0
        p.update(round(got / 2**20, 1), total, detail="downloading", unit="MB")
        time.sleep(0.2)
    r = subprocess.CompletedProcess(proc.args, proc.returncode, "", proc.stderr.read())
    proc.stderr.close()
    if r.returncode:
        part.unlink(missing_ok=True)
        raise ToolError(f"downloading {url} failed: {r.stderr.strip()}")
    p.detail("checking the download")
    got = _sha256(part)
    if got != sha256:
        part.unlink(missing_ok=True)
        raise ToolError(f"{url} has checksum {got}, expected {sha256}; not using it")
    part.replace(dest)
    return dest


def xcodegen() -> str:
    """Path of an xcodegen executable: the one on PATH, or the pinned release in the cache."""
    found = shutil.which("xcodegen")
    if found:
        return found
    home = CACHE / f"xcodegen-{XCODEGEN['version']}"
    exe = home / "xcodegen" / "bin" / "xcodegen"
    if exe.exists():
        return str(exe)
    with progress_mod.step(f"Downloading XcodeGen {XCODEGEN['version']}") as p:
        z = download(XCODEGEN["url"], CACHE / f"xcodegen-{XCODEGEN['version']}.zip", XCODEGEN["sha256"],
                     XCODEGEN["bytes"], p)
        p.detail("unpacking")
        tmp = Path(tempfile.mkdtemp(prefix="xcodegen-", dir=CACHE))
        r = subprocess.run(["unzip", "-q", str(z), "-d", str(tmp)], capture_output=True, text=True)
        if r.returncode or not (tmp / "xcodegen" / "bin" / "xcodegen").exists():
            shutil.rmtree(tmp, ignore_errors=True)
            raise ToolError(f"unpacking {z.name} failed: {r.stderr.strip()}")
    shutil.rmtree(home, ignore_errors=True)
    tmp.rename(home)
    return str(exe)


def python_xcframework() -> Path:
    """Python.xcframework from BeeWare's Python-Apple-support, downloaded and unpacked once."""
    root = CACHE / f"py{PYTHON_SUPPORT['python'].replace('.', '')}-ios"
    xcf = root / "Python.xcframework"
    versions = root / "VERSIONS"
    if (xcf / "Info.plist").exists() and versions.exists() and \
            f"Build: {PYTHON_SUPPORT['build']}" in versions.read_text():
        return xcf
    name = PYTHON_SUPPORT["url"].rsplit("/", 1)[1]
    with progress_mod.step(f"Downloading Python {PYTHON_SUPPORT['python']} for iOS") as p:
        tar = download(PYTHON_SUPPORT["url"], CACHE / name, PYTHON_SUPPORT["sha256"], PYTHON_SUPPORT["bytes"], p)
        p.detail("unpacking")
        tmp = Path(tempfile.mkdtemp(prefix="py-ios-", dir=CACHE))
        r = subprocess.run(["tar", "-xzf", str(tar), "-C", str(tmp)], capture_output=True, text=True)
        if r.returncode or not (tmp / "Python.xcframework" / "Info.plist").exists():
            shutil.rmtree(tmp, ignore_errors=True)
            raise ToolError(f"unpacking {name} failed: {r.stderr.strip() or 'no Python.xcframework in it'}")
    shutil.rmtree(root, ignore_errors=True)
    tmp.rename(root)
    return xcf


def metal_toolchain(run=None) -> tuple[bool, str]:
    """(installed, version line) for the iOS Metal compiler, which builds the GPU shaders.
    run(cmd) -> CompletedProcess with text output (default: subprocess.run, captured)."""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=60))
    try:
        r = run(["xcrun", "-sdk", "iphoneos", "metal", "--version"])
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0 and "metal version" in out.lower():
        return True, out.splitlines()[0]
    return False, out.splitlines()[-1] if out else "metal not found"


def install_metal_toolchain() -> bool:
    """Download Xcode's Metal toolchain component. Output goes to the terminal (it shows progress)."""
    r = subprocess.run(["xcodebuild", "-downloadComponent", "MetalToolchain"])
    return r.returncode == 0
