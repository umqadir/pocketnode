"""torch's iOS Metal shaders: the section patch and the framework name torch looks for."""

from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from pocketnode.provision import torch_metal

needs_clang = pytest.mark.skipif(shutil.which("xcrun") is None, reason="needs Xcode's clang")


def _dylib(tmp: Path, section: bytes | None, extra: list[str] = ()) -> Path:
    c = tmp / "x.c"
    c.write_text("int f(void) { return 1; }\n")
    out = tmp / "libx.dylib"
    args = ["xcrun", "clang", "-dynamiclib", "-arch", "arm64", *extra, str(c), "-o", str(out)]
    if section is not None:
        blob = tmp / "blob"
        blob.write_bytes(section)
        args.append(f"-Wl,-sectcreate,__TEXT,metal_basic,{blob}")
    subprocess.run(args, check=True, capture_output=True)
    return out


def _section(binary: Path, extra: int = 0) -> bytes:
    """The section's contents, plus `extra` bytes after its end."""
    data = binary.read_bytes()
    ncmds = struct.unpack_from("<I", data, 16)[0]
    off = 32
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, off)
        if cmd == 0x19:
            for i in range(struct.unpack_from("<I", data, off + 64)[0]):
                sec = off + 72 + 80 * i
                if data[sec: sec + 16].rstrip(b"\0") == b"metal_basic":
                    n, at = struct.unpack_from("<QI", data, sec + 40)
                    return data[at: at + n + extra]
        off += size
    raise AssertionError("no metal_basic section")


@needs_clang
def test_patch_replaces_section(tmp_path):
    lib = _dylib(tmp_path, b"\xab" * 4096)
    new = bytes(range(256)) * 3 + b"metallib" * 29
    assert len(new) == 1000
    (tmp_path / "new.metallib").write_bytes(new)
    torch_metal.patch(lib, tmp_path / "new.metallib")
    assert _section(lib) == new
    otool = subprocess.run(["otool", "-l", str(lib)], capture_output=True, text=True, check=True).stdout
    assert "size 0x00000000000003e8" in otool.split("sectname metal_basic")[1].split("Section")[0]
    assert _section(lib, 3096)[1000:] == b"\0" * 3096  # the rest of the old contents is zeroed


@needs_clang
def test_patch_thins_fat_binary(tmp_path):
    lib = _dylib(tmp_path, b"\xab" * 4096, ["-arch", "x86_64"])
    (tmp_path / "new.metallib").write_bytes(b"\7" * 100)
    torch_metal.patch(lib, tmp_path / "new.metallib")
    assert subprocess.run(["lipo", "-archs", str(lib)], capture_output=True, text=True).stdout.split() == ["arm64"]
    assert _section(lib) == b"\7" * 100


@needs_clang
def test_patch_too_large(tmp_path):
    lib = _dylib(tmp_path, b"\xab" * 4096)
    (tmp_path / "big.metallib").write_bytes(b"\1" * 4097)
    with pytest.raises(ValueError, match="4097.*4096"):
        torch_metal.patch(lib, tmp_path / "big.metallib")
    assert _section(lib) == b"\xab" * 4096


@needs_clang
def test_patch_no_section(tmp_path):
    lib = _dylib(tmp_path, None)
    (tmp_path / "m.metallib").write_bytes(b"\1" * 10)
    with pytest.raises(LookupError):
        torch_metal.patch(lib, tmp_path / "m.metallib")


def test_libtorch_cpu_framework_name(tmp_path):
    from pocketnode.provision.stage import Stager
    s = Stager.__new__(Stager)
    s.pk = tmp_path
    s.sdk, s.bundle_id = "iphoneos", "dev.pocketnode.app"
    assert s._framework_name(tmp_path / "torch/lib/libtorch_cpu.dylib") == "libtorch_cpu.dylib"
    assert s._framework_name(tmp_path / "torch/lib/libtorch.dylib") == "torch.lib.libtorch"
    assert s._framework_name(tmp_path / "torch/_C.cpython-313-darwin.so") == "torch._C"
    fw = tmp_path / "libtorch_cpu.dylib.framework"
    fw.mkdir()
    s._write_plist(fw, "libtorch_cpu.dylib")
    import plistlib
    plist = plistlib.loads((fw / "Info.plist").read_bytes())
    assert plist["CFBundleExecutable"] == "libtorch_cpu.dylib"
    assert plist["CFBundleIdentifier"] == "dev.pocketnode.app.libtorch-cpu.dylib"
