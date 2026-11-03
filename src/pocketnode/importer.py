"""Worker-side import hook: pure-Python modules come from the Mac on demand.

The Mac's environment is the source of truth. A module the worker doesn't
bundle, or bundles at a different version, is fetched from the Mac as a zip,
unpacked into a content-addressed cache and imported from there. Compiled
modules can't be fetched (they need an iOS build); a miss on one is recorded so
the Mac can re-run the task where the module exists.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.metadata as md
import io
import os
import shutil
import sys
import threading
import zipfile
from pathlib import Path
from typing import Callable

# fetch(name, have_hash) -> (info, zip_bytes | None)
Fetch = Callable[[str, str | None], tuple[dict, bytes | None]]


class RemoteFinder(importlib.abc.MetaPathFinder):
    def __init__(self, cache_dir: Path, fetch: Fetch, local_only: set[str] = frozenset(),
                 on_miss: Callable[[str], None] | None = None):
        self.on_miss = on_miss
        self.cache_dir = Path(cache_dir) / "pymods"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fetch = fetch
        self.manifest: dict[str, dict] = {}
        self.local_versions: dict[str, str] = {}
        self.local_only = set(local_only)
        self.resolved: dict[str, Path | None] = {}
        self.missing_compiled: dict[str, dict] = {}
        self.fetched: dict[str, str] = {}
        self._lock = threading.RLock()
        self._index = self._load_index()

    # The Mac sends its manifest on connect; a new manifest invalidates choices
    # made against the old one (modules already imported stay as they are).
    def set_manifest(self, manifest: dict[str, dict]) -> None:
        with self._lock:
            self.manifest = manifest
            self.resolved.clear()
            self.local_versions = _local_versions()

    def paths(self) -> list[str]:
        """Directories of modules fetched so far (a snapshot, safe from any thread)."""
        with self._lock:
            return [str(d) for d in self.resolved.values() if d]

    def _load_index(self) -> dict[str, str]:
        idx = {}
        for d in self.cache_dir.glob("*-*"):
            name, _, h = d.name.rpartition("-")
            if (d / ".complete").exists():
                idx.setdefault(name, h)
        return idx

    def _wants_remote(self, top: str) -> bool:
        if top in sys.builtin_module_names or top in sys.stdlib_module_names or top in self.local_only:
            return False
        info = self.manifest.get(top)
        if info is None:
            return not _locally_importable(top)
        if info["kind"] == "compiled":
            return False
        local = self.local_versions.get(_norm(info["dist"]))
        return local != info["version"]

    def find_spec(self, fullname, path=None, target=None):
        if path is not None or "." in fullname:
            return None
        with self._lock:
            if fullname in self.resolved:
                d = self.resolved[fullname]
                return importlib.machinery.PathFinder.find_spec(fullname, [str(d)]) if d else None
            if not self._wants_remote(fullname):
                self._note_compiled_miss(fullname)
                return None
            d = self._resolve(fullname)
            self.resolved[fullname] = d
            if d is None:
                return None
            if str(d) not in sys.path:
                sys.path.append(str(d))  # lets importlib.metadata see the dist-info
            return importlib.machinery.PathFinder.find_spec(fullname, [str(d)])

    def _note_compiled_miss(self, top: str) -> None:
        info = self.manifest.get(top)
        if info and info["kind"] == "compiled" and not _locally_importable(top):
            self.missing_compiled[top] = info
            if self.on_miss:
                self.on_miss(top)

    def _resolve(self, top: str) -> Path | None:
        have = self._index.get(top)
        try:
            info, data = self.fetch(top, have)
        except Exception:
            return self.cache_dir / f"{top}-{have}" if have else None
        kind = info.get("kind")
        if kind == "compiled":
            self.missing_compiled[top] = info
            if self.on_miss:
                self.on_miss(top)
            return None
        if kind != "pure":
            return None
        h = info["hash"]
        dest = self.cache_dir / f"{top}-{h}"
        if data is not None and not (dest / ".complete").exists():
            tmp = dest.with_name(dest.name + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                z.extractall(tmp)
            (tmp / ".complete").touch()
            shutil.rmtree(dest, ignore_errors=True)
            os.replace(tmp, dest)
            if have and have != h:
                shutil.rmtree(self.cache_dir / f"{top}-{have}", ignore_errors=True)
        self._index[top] = h
        self.fetched[top] = info.get("version", "?")
        return dest


def _local_versions() -> dict[str, str]:
    out = {}
    for d in md.distributions():
        n = d.metadata["Name"]
        if n:
            out[_norm(n)] = d.version
    return out


def _norm(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def _locally_importable(top: str) -> bool:
    for finder in sys.meta_path:
        if isinstance(finder, RemoteFinder):
            continue
        try:
            if finder.find_spec(top, None) is not None:
                return True
        except Exception:
            continue
    return False


def install(finder: RemoteFinder) -> None:
    for f in list(sys.meta_path):
        if isinstance(f, RemoteFinder):
            sys.meta_path.remove(f)
    sys.meta_path.insert(0, finder)
