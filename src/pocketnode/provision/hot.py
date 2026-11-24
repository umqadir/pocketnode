"""Add compiled packages to the phone while the app is running. No reinstall.

iOS loads native code from the app's data container if it is signed with the
same development certificate as the app and its signing identifier equals the
app's bundle identifier (measured on iOS 27.2; a signature that differs only
in the identifier is rejected). So a package is retagged in place, signed that
way, sent over the link and imported from the phone's site directory, in
seconds.

    from pocketnode.provision import hot
    hot.add(executor, ["lightgbm"])
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..sources import BUNDLE_ID, CONFIG_DIR
from . import app as app_mod
from . import stage as stage_mod

BATCH = 48 << 20


class CantAdd(RuntimeError):
    pass


def _norm(n: str) -> str:
    return n.lower().replace("-", "_").replace(".", "_")


def node_sdk(node) -> str | None:
    """Which SDK a node's binaries need: None means it runs this Mac's binaries as they are."""
    h = node.hello
    if h.get("platform") != "ios":
        return None
    return "iphonesimulator" if h.get("simulator") else "iphoneos"


def signing_identity() -> str:
    """The certificate that signed the installed app (added code must use the same one)."""
    built = app_mod.IOS_DIR / "build" / "Build" / "Products" / "Debug-iphoneos" / "PocketNode.app"
    if built.exists():
        r = subprocess.run(["codesign", "-dvv", str(built)], capture_output=True, text=True)
        for line in r.stderr.splitlines():
            if line.startswith("Authority=Apple Development"):
                return line.split("=", 1)[1]
    from ..bench import signing_identity as any_identity
    ident = any_identity()
    if not ident:
        raise CantAdd("no Apple Development signing identity in the keychain; run `pocketnode app install` first")
    return ident


def prepare(names: list[str], have: dict[str, str], sdk: str | None, out: Path) -> stage_mod.Report:
    """Stage the compiled distributions `names` need that the node doesn't have (at this version)."""
    have = {_norm(k): v for k, v in have.items()}

    def wanted(d) -> bool:
        if have.get(_norm(d.metadata["Name"])) == d.version:
            return False
        # Pure-Python dependencies are imported from the Mac on demand, as always.
        return any(str(f).endswith((".so", ".dylib")) for f in d.files or ())

    if sdk is None:  # a local stand-in: the Mac's binaries run there unchanged
        import shutil
        shutil.rmtree(out, ignore_errors=True)
        s = stage_mod.Stager.__new__(stage_mod.Stager)
        s.out, s.pk, s.report = out, out / "app_packages", stage_mod.Report("macosx")
        s.pk.mkdir(parents=True)
        for d in stage_mod.closure(names):
            if wanted(d):
                s._copy_dist(d)
        return s.report
    s = stage_mod.Stager(sdk, out, bundle_id=app_mod.config().get("bundle_id", BUNDLE_ID), inplace=True)
    rep = s.stage(names, only=wanted)
    bins = [p for p in s.pk.rglob("*") if p.is_file() and not p.is_symlink() and stage_mod.is_macho(p)]
    if sdk == "iphoneos":
        ident, bid = signing_identity(), app_mod.config().get("bundle_id", BUNDLE_ID)
        args = ["-s", ident, "--identifier", bid]
    else:
        args = ["-s", "-"]  # the simulator doesn't check who signed

    def sign(p: Path) -> None:
        r = subprocess.run(["codesign", "-f", "--timestamp=none", *args, str(p)], capture_output=True, text=True)
        if r.returncode:
            raise CantAdd(f"signing {p.name} failed: {r.stderr.strip()}")

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(sign, bins))
    return rep


def add(ex, names: list[str], on: str = "phone", timeout: float = 1800, quiet: bool = False) -> dict:
    """Put compiled packages on a connected node. Returns {"added": {dist: version}, "seconds", "mb", ...}."""
    node = next((n for n in ex.nodes.values() if on in (n.kind, n.name, n.id)), None)
    if node is None:
        raise CantAdd(f"no connected node matches {on!r}")
    t0 = time.time()
    out = Path(tempfile.mkdtemp(prefix="pn-hot-"))
    try:
        rep = prepare(list(names), node.hello.get("packages", {}), node_sdk(node), out)
        if not rep.dists:
            return {"added": {}, "seconds": 0.0, "send_s": 0.0, "mb": 0.0, "excluded": rep.excluded,
                    "warnings": rep.warnings, "failed": {}, "restart": False, "note": "already there"}
        pk = out / "app_packages"
        files = sorted(p for p in pk.rglob("*") if p.is_file())
        total = sum(p.stat().st_size for p in files)
        avail = node.hello.get("disk_free")
        if avail and total > avail - (1 << 30):
            raise CantAdd(f"{total / 2**20:.0f} MB wouldn't fit in the {avail / 2**20:.0f} MB free on {node.name}")
        if not quiet:
            ex._note(None, f"pocketnode: adding {', '.join(rep.dists)} to {node.name} ({total / 2**20:.0f} MB)…")
        t1 = time.time()
        req = uuid.uuid4().hex
        reply = ex.expect(req)
        batch, size, seq = [], 0, 0

        def flush(last: bool) -> None:
            nonlocal batch, size, seq
            node.post({"t": "install", "req": req, "seq": seq, "last": last, "dists": rep.dists,
                       "files": [{"p": str(p.relative_to(pk)), "x": os.access(p, os.X_OK)} for p in batch]},
                      [p.read_bytes() for p in batch])
            batch, size, seq = [], 0, seq + 1

        for p in files:
            n = p.stat().st_size
            if batch and size + n > BATCH:
                flush(False)
            batch.append(p)
            size += n
        flush(True)
        msg = reply(timeout)
        if msg is None:
            raise CantAdd(f"{node.name} didn't confirm the install in time")
        if msg.get("error"):
            raise CantAdd(msg["error"])
        msg["failed"] = {k: v for k, v in (msg.get("failed") or {}).items() if _imports_here(k)}
    finally:
        import shutil
        shutil.rmtree(out, ignore_errors=True)
    ex.packages_changed(node, msg.get("packages", {}), rep.dists, msg.get("failed"))
    result = {"added": rep.dists, "seconds": round(time.time() - t0, 1), "send_s": round(time.time() - t1, 1),
              "mb": round(total / 2**20, 1), "excluded": rep.excluded, "warnings": rep.warnings,
              "failed": msg.get("failed", {}), "restart": bool(msg.get("restart"))}
    app_mod._record({"event": "hot", "packages": sorted(rep.dists), "seconds": result["seconds"], "mb": result["mb"]})
    if msg.get("restart"):
        # A module of one of these packages was already imported at its old
        # version; a native library can't be swapped under a running process.
        ex._note(None, f"pocketnode: restarting the app on {node.name} to switch to the new "
                       f"{', '.join(msg['restart'])}")
        node.post({"t": "restart"})
    if not quiet:
        bad = result["failed"]
        ex._note(None, f"pocketnode: {', '.join(rep.dists)} now on {node.name} ({result['seconds']} s)"
                 + (f"; couldn't import {', '.join(bad)} there, so tasks using it stay on the Mac" if bad else ""))
    return result


def _imports_here(module: str) -> bool:
    """Can this Mac import the module? A package's optional extra that needs
    something not installed here either (duckdb's ADBC driver) isn't a failure
    of the phone."""
    import sys
    r = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, timeout=120)
    return r.returncode == 0


def remove(ex, names: list[str], on: str = "phone", timeout: float = 120) -> dict:
    node = next((n for n in ex.nodes.values() if on in (n.kind, n.name, n.id)), None)
    if node is None:
        raise CantAdd(f"no connected node matches {on!r}")
    req = uuid.uuid4().hex
    reply = ex.expect(req)
    node.post({"t": "uninstall", "req": req, "dists": list(names)})
    msg = reply(timeout)
    if msg is None:
        raise CantAdd(f"{node.name} didn't answer")
    ex.packages_changed(node, msg.get("packages", {}), {})
    if msg.get("restart"):
        node.post({"t": "restart"})
    return {"removed": msg.get("removed", []), "bundled": msg.get("bundled", [])}


def estimate(names: list[str], bandwidth: float = 30e6) -> dict:
    """Seconds to add `names`, from sizes and measured history where there is some."""
    import importlib.metadata as md
    mb = 0.0
    for d in stage_mod.closure(names):
        if any(str(f).endswith((".so", ".dylib")) for f in d.files or ()):
            mb += sum(Path(d.locate_file(f)).stat().st_size for f in d.files or ()
                      if Path(d.locate_file(f)).is_file()) / 2**20
    hist = []
    if app_mod.HISTORY.exists():
        hist = [json.loads(l) for l in app_mod.HISTORY.read_text().splitlines() if l.strip()]
    hot = [h for h in hist if h.get("event") == "hot" and h.get("mb")]
    if hot:
        rate = sorted(h["seconds"] / h["mb"] for h in hot)[len(hot) // 2]
        return {"seconds": round(3 + mb * rate), "mb": round(mb), "measured": True}
    return {"seconds": round(5 + mb / (bandwidth / 2**20) + mb * 0.05), "mb": round(mb), "measured": False}
