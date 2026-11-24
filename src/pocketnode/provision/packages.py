"""What's on the phone, what could be, and what changing it costs.

    pn.packages()                      # table: Mac env vs phone, with status
    pn.packages.add("lightgbm")        # onto the running app, in seconds (no restart)
    pn.packages.add("lightgbm", bundle=True)  # rebuild the app with it baked in (asks first)
    pn.packages.always_mac("lightgbm") # never offer it again; run it on the Mac

Pure-Python packages never need installing: the phone imports them from the
Mac on demand. Compiled packages are retagged from the Mac's own build, so the
phone runs exactly the Mac's versions. Each is checked statically against the
iOS SDK first, and anything that can't work says why.
"""

from __future__ import annotations

import contextlib
import importlib.metadata as md
import json
import time
from dataclasses import dataclass
from pathlib import Path

from ..sources import CONFIG_DIR
from . import app as app_mod

DEFAULT_STACK = ["numpy", "pandas", "scipy", "scikit-learn", "statsmodels", "joblib", "threadpoolctl", "torch"]
CHECK_CACHE = CONFIG_DIR / "check-cache.json"
MISSES = CONFIG_DIR / "misses.json"


def record_miss(package: str) -> None:
    """Remember that a task wanted a compiled package the phone lacks (shown in the selector)."""
    try:
        d = json.loads(MISSES.read_text()) if MISSES.exists() else {}
        e = d.setdefault(package, {"n": 0})
        e["n"] += 1
        e["last"] = time.time()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        MISSES.write_text(json.dumps(d))
    except (OSError, ValueError):
        pass


def recent_misses(days: float = 14) -> dict[str, int]:
    try:
        d = json.loads(MISSES.read_text())
    except (OSError, ValueError):
        return {}
    return {k.lower(): v["n"] for k, v in d.items() if time.time() - v.get("last", 0) < days * 86400}


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
    measured = bool(builds) and bool(installs)
    build_s = sorted(h["build_s"] + h["stage_s"] for h in builds)[len(builds) // 2] if builds else 90.0
    install_s = sorted(h["seconds"] for h in installs)[len(installs) // 2] if installs else 40.0
    sizes = {r.name.lower(): r.size_mb for r in table(check=False)}
    extra_mb = sum(sizes.get(n.lower(), 0) for n in names)
    build_s += extra_mb * 0.05  # staging/signing scales with size (~20 MB/s, measured order of magnitude)
    install_s += extra_mb * 0.02
    return {"build_s": round(build_s), "install_s": round(install_s), "measured": measured}


@contextlib.contextmanager
def _live(executor=None):
    """An executor with the phone connected (a temporary one if needed), or None."""
    import pocketnode
    ex, temp = executor or pocketnode._current, None
    if ex is None:
        prev = pocketnode._current
        ex = temp = pocketnode.connect(mac_workers=0, notes=False, auto_add=False)
        pocketnode._current = prev  # connect() makes it current; it's only borrowed here
        ex.wait_for("phone", 30)
    try:
        yield ex if any(n.kind == "phone" for n in ex.nodes.values()) else None
    finally:
        if temp is not None:
            end = time.monotonic() + 2  # let a queued restart request reach the phone
            while time.monotonic() < end and any(not n.outbox.empty() for n in temp.nodes.values()):
                time.sleep(0.05)
            temp.shutdown(wait=False)


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

    def add(self, *names: str, yes: bool = False, when_idle: bool = False, sdk: str | None = None,
            bundle: bool = False, executor=None):
        """Put compiled packages on the running phone app (seconds, nothing restarts).
        bundle=True instead rebuilds the app with them baked in (asks first)."""
        if not bundle:
            from . import hot
            with _live(executor) as ex:
                if ex is not None:
                    est = hot.estimate(list(names))
                    print(f"adding {', '.join(names)}: about {est['seconds']} s, {est['mb']} MB"
                          f"{' (measured)' if est['measured'] else ' (estimate)'}")
                    r = hot.add(ex, list(names), quiet=True)
                    if not r["added"]:
                        print(f"nothing to add: {', '.join(names)} already on the phone "
                              "(or pure Python, which the phone imports from the Mac on demand)")
                    else:
                        got = ", ".join(f"{k} {v}" for k, v in r["added"].items())
                        print(f"added {got} in {r['seconds']} s ({r['mb']} MB)")
                    for m, why in r.get("excluded", {}).items():
                        print(f"  {m} can't run on iOS: {why}")
                    for m, why in r.get("failed", {}).items():
                        print(f"  {m} is there but failed to import ({why}); tasks using it run on the Mac")
                    if r.get("restart"):
                        print("  an older version was already loaded: the app restarts to switch")
                    return r
            print("no phone reachable; adding it to the app build instead")
        return self._change(add=list(names), yes=yes, when_idle=when_idle, sdk=sdk)

    def remove(self, *names: str, yes: bool = False, sdk: str | None = None, bundle: bool = False,
               executor=None):
        """Take packages added at runtime off the phone. bundle=True rebuilds the app without them."""
        if not bundle:
            from . import hot
            with _live(executor) as ex:
                if ex is not None:
                    r = hot.remove(ex, list(names))
                    if r["removed"]:
                        print(f"removed {', '.join(r['removed'])}")
                    for b in r["bundled"]:
                        print(f"{b} is part of the app build; `pocketnode packages remove --bundle {b}` "
                              "rebuilds without it")
                    return r
            print("no phone reachable; removing it from the app build instead")
        return self._change(remove=list(names), yes=yes, sdk=sdk)

    def always_mac(self, *names: str):
        cfg = app_mod.config()
        app_mod.save_config(always_mac=sorted(set(cfg.get("always_mac", [])) | set(names)))
        print(f"won't offer {', '.join(names)} again; tasks needing them run on the Mac")

    def select(self, sdk: str | None = None, bundle: bool = False):
        """Interactive checklist: what's on the phone, what could be, what changing it costs.
        Changes go to the running app; bundle=True rebuilds the app with exactly the ticked set."""
        rows = [r for r in table(check=True) if r.kind == "compiled"]
        on_phone = phone_packages()
        if on_phone is None:  # first use: a sensible scientific stack is pre-checked
            have = {r.name.lower() for r in rows}
            checked = {n for n in map(str.lower, DEFAULT_STACK) if n in have}
            first = True
        else:
            checked = {r.name.lower() for r in rows if r.status == "on phone"}
            first = False
        chosen = _checklist(rows, set(checked), recent_misses(), first)  # a copy: the checklist edits it
        if chosen is None:
            print("left as is")
            return
        names = {r.name.lower(): r.name for r in rows}
        if bundle:
            return self._change(new=sorted(names[n] for n in chosen), sdk=sdk, before=None if first else checked)
        had = set() if first else checked
        added = sorted(names[n] for n in chosen - had)
        dropped = sorted(names.get(n, n) for n in had - chosen)
        if not added and not dropped:
            print("nothing to change")
            return
        if added:
            self.add(*added, sdk=sdk)
        if dropped:
            self.remove(*dropped, sdk=sdk)

    def _change(self, add=(), remove=(), yes=False, when_idle=False, sdk=None, new=None, before=None):
        if new is not None:
            # From the selector: `new` is everything ticked, `before` what the phone
            # had (which includes dependencies of the packages named in the config).
            had, want = set(before or ()), {n.lower() for n in new}
            added, dropped = sorted(n for n in new if n.lower() not in had), sorted(had - want)
        else:
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


def _checklist(rows: list[Row], checked: set[str], misses: dict[str, int], first: bool) -> set[str] | None:
    """Curses checklist. Returns the chosen package names (lowercase), or None if cancelled."""
    import curses

    start = set(checked)
    sizes = {r.name.lower(): r.size_mb for r in rows}
    rows = sorted(rows, key=lambda r: (r.name.lower() not in checked and r.name.lower() not in misses,
                                       r.ios == "blocked", r.name.lower()))

    def ui(scr):
        curses.curs_set(0)
        curses.use_default_colors()
        pos = top = 0
        while True:
            h, w = scr.getmaxyx()
            body = h - 5
            top = min(max(top, pos - body + 1), pos)
            scr.erase()
            head = ("First install: a default scientific stack is pre-checked." if first
                    else "Checked = on the phone now.")
            scr.addnstr(0, 0, f"{head}  space: toggle   enter: apply   q: cancel", w - 1, curses.A_BOLD)
            for i, r in enumerate(rows[top:top + body]):
                n = r.name.lower()
                box = "[x]" if n in checked else "[ ]"
                if r.ios == "blocked":
                    box = " - "
                flag = f"  needed {misses[n]}x recently" if n in misses and n not in start else ""
                note = {"ok": "", "shim": "works with stand-ins for a few macOS-only calls",
                        "partial": r.detail, "blocked": "can't run on iOS: " + r.detail}.get(r.ios, r.detail)
                line = f"{box} {r.name[:26]:26} {r.version[:12]:12} {r.size_mb:6.0f} MB{flag}  {note}"
                scr.addnstr(2 + i, 0, line, w - 1, curses.A_REVERSE if top + i == pos else 0)
            add, drop = sorted(checked - start), sorted(start - checked)
            mb = sum(sizes.get(n, 0) for n in add)
            est = estimate(add) if (add or drop or first) else None
            foot = ("no changes" if est is None else
                    f"+{', '.join(add) or '-'}  -{', '.join(drop) or '-'}   about {est['build_s'] + est['install_s']} s"
                    f"{'' if est['measured'] else ' (first-time estimate)'}, {mb:.0f} MB more; the app restarts")
            scr.addnstr(h - 2, 0, foot, w - 1, curses.A_BOLD)
            scr.refresh()
            k = scr.getch()
            if k in (ord("q"), 27):
                return None
            if k in (curses.KEY_DOWN, ord("j")):
                pos = min(pos + 1, len(rows) - 1)
            elif k in (curses.KEY_UP, ord("k")):
                pos = max(pos - 1, 0)
            elif k == curses.KEY_NPAGE:
                pos = min(pos + body, len(rows) - 1)
            elif k == curses.KEY_PPAGE:
                pos = max(pos - body, 0)
            elif k == ord(" ") and rows[pos].ios != "blocked":
                checked.symmetric_difference_update({rows[pos].name.lower()})
            elif k in (curses.KEY_ENTER, 10, 13):
                return checked

    return curses.wrapper(ui)


packages = Packages()
