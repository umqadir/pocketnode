"""The Mac's Python environment as workers see it.

`manifest()` lists every importable top-level module with its distribution,
version and whether it contains compiled code. `package_module()` zips a pure
Python module (plus its dist-info) for a worker that wants to import it.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as md
import importlib.util
import io
import os
import sys
import threading
import zipfile
from functools import lru_cache
from pathlib import Path

NATIVE_SUFFIXES = (".so", ".dylib", ".pyd", ".dll")
_SKIP_DIRS = {"__pycache__", ".git", "tests", "test"}  # tests aren't needed at import
_lock = threading.Lock()


def _dist_has_native(dist: md.Distribution) -> bool:
    for f in dist.files or ():
        if str(f).endswith(NATIVE_SUFFIXES):
            return True
    return False


@lru_cache(maxsize=1)
def manifest() -> dict[str, dict]:
    """{top_level_module: {dist, version, kind}} for installed distributions."""
    out: dict[str, dict] = {}
    pkgs = md.packages_distributions()
    dists: dict[str, md.Distribution] = {}
    for d in md.distributions():
        name = d.metadata["Name"]
        if name:
            dists.setdefault(_norm(name), d)
    for top, names in pkgs.items():
        if "." in top or top == "__pycache__":
            continue
        d = dists.get(_norm(names[0]))
        if d is None:
            continue
        native = _top_has_native(d, top)
        out[top] = {
            "dist": d.metadata["Name"],
            "version": d.version,
            "kind": "compiled" if native else "pure",
        }
    return out


def refresh() -> None:
    manifest.cache_clear()
    _package_cache.clear()


def _norm(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def _top_has_native(d: md.Distribution, top: str) -> bool:
    for f in d.files or ():
        parts = f.parts
        if parts and (parts[0] == top or len(parts) == 1 and parts[0].split(".")[0] == top):
            if str(f).endswith(NATIVE_SUFFIXES):
                return True
    return False


def locate(top: str) -> dict:
    """Where the Mac would import `top` from, and what kind of module it is."""
    if top in sys.builtin_module_names:
        return {"kind": "builtin"}
    if top in sys.stdlib_module_names:
        return {"kind": "stdlib"}
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return {"kind": "absent"}
    info = manifest().get(top, {})
    if spec.submodule_search_locations:
        locs = [Path(p) for p in spec.submodule_search_locations]
        native = any(_tree_has_native(p) for p in locs)
        return {"kind": "compiled" if native else "pure", "paths": [str(p) for p in locs],
                "package": True, **{k: info[k] for k in ("dist", "version") if k in info}}
    origin = spec.origin or ""
    if origin.endswith(NATIVE_SUFFIXES):
        return {"kind": "compiled", "paths": [origin], **{k: info[k] for k in ("dist", "version") if k in info}}
    if origin.endswith(".py"):
        return {"kind": "pure", "paths": [origin], "package": False,
                **{k: info[k] for k in ("dist", "version") if k in info}}
    return {"kind": "absent"}


def _tree_has_native(root: Path) -> bool:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        if any(f.endswith(NATIVE_SUFFIXES) for f in filenames):
            return True
    return False


_package_cache: dict[str, tuple[tuple, str, bytes]] = {}


def _signature(paths: list[Path]) -> tuple:
    sig = []
    for root in paths:
        if root.is_file():
            st = root.stat()
            sig.append((str(root), st.st_mtime_ns, st.st_size))
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for f in filenames:
                st = os.stat(os.path.join(dirpath, f))
                sig.append((dirpath, f, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def package_module(top: str) -> tuple[dict, bytes | None]:
    """Zip a pure-Python top-level module for shipping. Cached until files change."""
    info = locate(top)
    if info["kind"] != "pure":
        return info, None
    paths = [Path(p) for p in info["paths"]]
    sig = _signature(paths)
    with _lock:
        hit = _package_cache.get(top)
        if hit and hit[0] == sig:
            return {**info, "hash": hit[1]}, hit[2]
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for p in paths:
            if p.is_file():
                z.write(p, p.name)
                continue
            base = p.parent
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(d for d in dirnames if d not in ("__pycache__", ".git"))
                for f in sorted(filenames):
                    if f.endswith((".pyc", ".pyo")):
                        continue
                    full = os.path.join(dirpath, f)
                    z.write(full, os.path.relpath(full, base))
        dist = info.get("dist")
        if dist:
            try:
                d = md.distribution(dist)
                for f in d.files or ():
                    s = str(f)
                    if ".dist-info/" in s and s.split("/")[-1] in ("METADATA", "entry_points.txt", "top_level.txt"):
                        full = d.locate_file(f)
                        if os.path.exists(full):
                            z.write(full, s)
            except md.PackageNotFoundError:
                pass
    data = bio.getvalue()
    h = hashlib.sha256(data).hexdigest()[:24]
    with _lock:
        _package_cache[top] = (sig, h, data)
    return {**info, "hash": h}, data
