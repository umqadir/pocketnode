"""What's on the phone, what could be, and what changing it costs.

    pn.packages()                      # table: Mac env vs phone, with status
    pn.packages.add("lightgbm")        # stage, rebuild, reinstall (asks first)
    pn.packages.always_mac("lightgbm") # never offer it again; run it on the Mac

Pure-Python packages never need installing: the phone imports them from the
Mac on demand. Compiled packages are retagged from the Mac's own build, so the
phone runs exactly the Mac's versions. Each is checked statically against the
iOS SDK first, and anything that can't work says why.
"""

from __future__ import annotations

import importlib.metadata as md
import json
import time
from dataclasses import dataclass
from pathlib import Path

from ..sources import CONFIG_DIR
from . import app as app_mod

DEFAULT_STACK = ["numpy", "pandas", "scipy", "scikit-learn", "statsmodels", "joblib", "threadpoolctl", "torch"]
CHECK_CACHE = CONFIG_DIR / "check-cache.json"


@dataclass
class Row:
    name: str
    version: str
    kind: str          # pure | compiled
    status: str        # on phone | not on phone | auto (pure)
    ios: str           # ok | shim | partial | blocked | -
    detail: str = ""
    size_mb: float = 0.0


def _dist_files(d: md.Distribution) -> list[Path]:
    return [Path(d.locate_file(f)) for f in d.files or ()]


def _check_dist(d: md.Distribution) -> tuple[str, str]:
    """Static iOS verdict for a compiled distribution, cached by name+version."""
    key = f"{d.metadata['Name']}=={d.version}"
    cache = json.loads(CHECK_CACHE.read_text()) if CHECK_CACHE.exists() else {}
    if key in cache:
        return tuple(cache[key])
    from . import shims, symcheck
    from .stage import is_macho
    bad, shimmed, n = [], [], 0
    for p in _dist_files(d):
        if not p.is_file() or not is_macho(p):
            continue
        n += 1
        try:
            r = symcheck.analyze(str(p))
        except Exception as e:  # noqa: BLE001
            bad.append(f"{p.name}: check failed ({e})")
            continue
        syms = [s for s, _ in r["missing_syms"]]
        weak = [l for l in r["missing_libs"] if l.startswith("/System/Library/Frameworks/")]
        hard = [l for l in r["missing_libs"] if l not in weak]
        if hard:
            bad.append(f"{p.name}: links {', '.join(Path(l).name for l in hard)}")
        elif (weak or syms) and all(shims.can_shim([s], l) for s, l in r["missing_syms"]):
            shimmed.append(p.name)
        elif syms:
            bad.append(f"{p.name}: needs {', '.join(syms[:3])}")
        if weak and not hard:
            shimmed.append(f"{p.name} without {', '.join(Path(l).name for l in weak)}")
    if not bad:
        verdict = ("shim" if shimmed else "ok", f"{n} binaries" + (f", {len(shimmed)} shimmed" if shimmed else ""))
    elif len(bad) < n:
        verdict = ("partial", f"{len(bad)}/{n} modules can't load on iOS: " + "; ".join(bad[:3]))
    else:
        verdict = ("blocked", "; ".join(bad[:3]))
    cache[key] = verdict
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CHECK_CACHE.write_text(json.dumps(cache))
    return verdict


def phone_packages(executor=None) -> dict[str, str] | None:
    """Packages on the phone: from the live phone if connected, else the last build."""
    ex = executor
    if ex is None:
        from .. import _current
        ex = _current
    if ex is not None:
        for n in ex.nodes.values():
            if n.kind == "phone":
                return dict(n.hello.get("packages", {}))
    rep = Path(app_mod.stage_mod.STAGE_ROOT) / app_mod.config().get("sdk", "iphoneos") / "stage-report.json"
    if rep.exists():
        return json.loads(rep.read_text())["dists"]
    return None


def table(check: bool = True, executor=None) -> list[Row]:
    on_phone = {k.lower(): v for k, v in (phone_packages(executor) or {}).items()}
    rows = []
    for d in sorted(md.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
        name = d.metadata["Name"]
        if not name:
            continue
        files = _dist_files(d)
        compiled = any(str(f).endswith((".so", ".dylib")) for f in files)
        size = sum(f.stat().st_size for f in files if f.is_file()) / 2**20
        if not compiled:
            rows.append(Row(name, d.version, "pure", "auto", "-", "imported from the Mac on demand", size))
            continue
        have = on_phone.get(name.lower())
        status = "on phone" if have == d.version else (f"phone has {have}" if have else "not on phone")
        ios, detail = _check_dist(d) if check else ("?", "")
        rows.append(Row(name, d.version, "compiled", status, ios, detail, size))
    return rows


def estimate(names: list[str]) -> dict:
    """Seconds to stage+build+install, from measured history where available."""
    hist = []
    if app_mod.HISTORY.exists():
        hist = [json.loads(l) for l in app_mod.HISTORY.read_text().splitlines() if l.strip()]
    builds = [h for h in hist if h.get("event") == "build"]
    installs = [h for h in hist if h.get("event") == "install"]
    measured = bool(builds)
    build_s = sorted(h["build_s"] + h["stage_s"] for h in builds)[len(builds) // 2] if builds else 90.0
    install_s = sorted(h["seconds"] for h in installs)[len(installs) // 2] if installs else 40.0
    sizes = {r.name.lower(): r.size_mb for r in table(check=False)}
    extra_mb = sum(sizes.get(n.lower(), 0) for n in names)
    build_s += extra_mb * 0.05  # staging/signing scales with size (~20 MB/s, measured order of magnitude)
    install_s += extra_mb * 0.02
    return {"build_s": round(build_s), "install_s": round(install_s), "measured": measured}


class Packages:
    """`pn.packages` — call it for the table; methods change what's on the phone."""

    def __call__(self, check: bool = True):
        rows = table(check)
        lines = [f"{'package':28} {'version':14} {'kind':9} {'phone':16} iOS"]
        for r in rows:
            if r.kind == "pure":
                continue
            lines.append(f"{r.name[:28]:28} {r.version[:14]:14} {r.kind:9} {r.status[:16]:16} {r.ios}  {r.detail[:70]}")
        n_pure = sum(r.kind == "pure" for r in rows)
        lines.append(f"(+{n_pure} pure-Python packages: always available, imported from the Mac on demand)")
        print("\n".join(lines))

    def add(self, *names: str, yes: bool = False, when_idle: bool = False, sdk: str | None = None):
        return self._change(add=list(names), yes=yes, when_idle=when_idle, sdk=sdk)

    def remove(self, *names: str, yes: bool = False, sdk: str | None = None):
        return self._change(remove=list(names), yes=yes, sdk=sdk)

    def always_mac(self, *names: str):
        cfg = app_mod.config()
        app_mod.save_config(always_mac=sorted(set(cfg.get("always_mac", [])) | set(names)))
        print(f"won't offer {', '.join(names)} again; tasks needing them run on the Mac")

    def _change(self, add=(), remove=(), yes=False, when_idle=False, sdk=None):
        cur = app_mod.packages()
        new = sorted((set(cur) | set(add)) - set(remove))
        added, dropped = sorted(set(new) - set(cur)), sorted(set(cur) - set(new))
        if not added and not dropped:
            print("nothing to change")
            return
        est = estimate(added)
        label = "" if est["measured"] else " (first-time estimate)"
        print(f"change: +{', '.join(added) or '-'}  -{', '.join(dropped) or '-'}")
        print(f"about {est['build_s'] + est['install_s']} s{label}: build ~{est['build_s']} s, "
              f"install/restart ~{est['install_s']} s")
        print("reinstalling restarts the app: running phone tasks are re-run elsewhere and the phone's "
              "in-memory data cache is dropped (disk caches survive).")
        if not yes:
            if input("go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
                print("left as is")
                return
        if when_idle:
            from .. import _current
            while _current and any(n.running for n in _current.nodes.values() if n.kind == "phone"):
                time.sleep(1)
        app_mod.save_config(packages=new)
        sdk = sdk or app_mod.config().get("sdk", "iphoneos")
        p = app_mod.build(sdk, new)
        if sdk == "iphonesimulator":
            app_mod.install_sim(p)
        else:
            app_mod.install_device(p)
        print("done; the phone reconnects on its own")


packages = Packages()
