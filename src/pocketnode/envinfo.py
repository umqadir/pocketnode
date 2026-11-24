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
import re
import sys
import threading
import zipfile
from functools import lru_cache
from pathlib import Path

NATIVE_SUFFIXES = (".so", ".dylib", ".pyd", ".dll")
_SKIP_DIRS = {"__pycache__", ".git", "tests", "test"}  # tests aren't needed at import
_lock = threading.Lock()


_REQ = re.compile(r"^Requires-Dist:\s*([A-Za-z0-9][A-Za-z0-9._-]*)([^\n]*)$", re.M)
_SKIP_TOP = (".dist-info", ".data", ".egg-info", ".pth")


def _scan_dist(d: md.Distribution):
    """(name, version, {top-level module: has native code}, required dist names) for one distribution.

    Reads METADATA and RECORD as text. importlib.metadata's own accessors build
    an object per file and take over ten seconds on a full scientific
    environment; this takes a fraction of one.
    """
    meta = d.read_text("METADATA") or d.read_text("PKG-INFO") or ""
    head = meta.split("\n\n", 1)[0]
    name = version = None
    for line in head.splitlines():
        if name is None and line.startswith("Name:"):
            name = line[5:].strip()
        elif version is None and line.startswith("Version:"):
            version = line[8:].strip()
    if not name:
        return None
    requires = {_norm(m.group(1)) for m in _REQ.finditer(head) if "extra ==" not in m.group(2)}
    tops: dict[str, bool] = {}
    record = d.read_text("RECORD")
    if record is None:  # egg-style install
        paths = [str(f) for f in d.files or ()]
    else:
        paths = [(line[1:line.index('"', 1)] if line.startswith('"') else line.split(",", 1)[0])
                 for line in record.splitlines() if line]
    for path in paths:
        first, sep, _ = path.partition("/")
        if first in ("..", "__pycache__", "bin", "") or first.endswith(_SKIP_TOP):
            continue
        native = path.endswith(NATIVE_SUFFIXES)
        if not sep:
            if not (native or first.endswith(".py")):
                continue
            first = first.split(".")[0]
        tops[first] = tops.get(first, False) or native
    return name, version or "0", tops, requires


@lru_cache(maxsize=1)
def _scan() -> tuple[dict, dict, dict]:
    """(manifest, {dist: required dists}, {dist: has native code})."""
    out: dict[str, dict] = {}
    requires: dict[str, set[str]] = {}
    native: dict[str, bool] = {}
    seen = set()
    for d in md.distributions():
        try:
            got = _scan_dist(d)
        except Exception:  # noqa: BLE001 - a broken dist-info shouldn't hide the rest
            got = None
        if got is None:
            continue
        name, version, tops, req = got
        key = _norm(name)
        if key in seen:  # an older copy later on sys.path
            continue
        seen.add(key)
        requires[key] = req
        native[key] = any(tops.values())
        for top, has_native in tops.items():
            cur = out.get(top)
            if cur is None:
                out[top] = {"dist": name, "version": version, "kind": "compiled" if has_native else "pure"}
            elif has_native:  # a namespace shared by several distributions
                cur["kind"] = "compiled"
    return out, requires, native


def manifest() -> dict[str, dict]:
    """{top_level_module: {dist, version, kind}} for installed distributions."""
    return _scan()[0]


@lru_cache(maxsize=None)
def needs_native(dist: str) -> bool:
    """Does this distribution, or anything it requires, contain compiled code?

    A pure-Python package built on a compiled one (polars on its runtime, seaborn
    on numpy) can't run where compiled modules can't load.
    """
    _, requires, native = _scan()
    todo, seen = [_norm(dist)], set()
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        if native.get(n):
            return True
        todo.extend(requires.get(n, ()))
    return False


def refresh() -> None:
    _scan.cache_clear()
    needs_native.cache_clear()
    _package_cache.clear()


def _norm(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


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


@lru_cache(maxsize=1)
def source_version() -> str:
    """A fingerprint of pocketnode's own source. The Mac and the phone app each
    report theirs; when they differ, the app was built from an older checkout."""
    h = hashlib.sha256()
    root = Path(__file__).resolve().parent
    for f in sorted(root.rglob("*.py")):
        parts = f.relative_to(root).parts
        if "provision" in parts or parts[0] == "ios" or f.name in ("cli.py", "sources.py"):
            continue  # Mac-only code (and the app's sources): the phone's copy of pocketnode lacks them
        h.update(f.relative_to(root).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]
