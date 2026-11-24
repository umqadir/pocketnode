"""Package management: estimates, the miss log, the table, add/remove on a stand-in
phone, the config, and the interactive checklist driven through a real terminal."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Keep test runs out of the real config (set before pocketnode reads it at import).
os.environ.setdefault("POCKETNODE_HOME", tempfile.mkdtemp(prefix="pn-test-home-"))
os.environ.setdefault("POCKETNODE_STAGE", tempfile.mkdtemp(prefix="pn-test-stage-"))

import pocketnode  # noqa: E402
from pocketnode.errors import PlacementError  # noqa: E402
from pocketnode.provision import app as app_mod  # noqa: E402
from pocketnode.provision import hot, packages  # noqa: E402
from pocketnode.provision import stage as stage_mod  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SIM_PY = ROOT / ".phone-sim" / "bin" / "python"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A private config folder per test (another test module may have imported pocketnode
    first, fixing CONFIG_DIR), and no way to reach the real phone."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(app_mod, "CONFIG_DIR", h)
    monkeypatch.setattr(app_mod, "HISTORY", h / "build-history.jsonl")
    monkeypatch.setattr(packages, "CONFIG_DIR", h)
    monkeypatch.setattr(packages, "MISSES", h / "misses.json")
    monkeypatch.setattr(packages, "CHECK_CACHE", h / "check-cache.json")
    monkeypatch.setattr(stage_mod, "STAGE_ROOT", tmp_path / "stage")
    monkeypatch.setattr(pocketnode, "_current", None)

    def no_connect(*a, **kw):
        raise AssertionError("tests must not connect to the real phone")

    monkeypatch.setattr(pocketnode, "connect", no_connect)
    return h


def _start_standin(tmp_path, port=None):
    if port is None:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
    tok = tmp_path / "token"
    tok.write_text("t0ken")
    proc = subprocess.Popen([str(SIM_PY), "-m", "pocketnode.worker", "--listen", f"127.0.0.1:{port}",
                             "--token-file", str(tok), "--phone", "--kind", "phone", "--name", "standalone",
                             "--cache-dir", str(tmp_path / "cache")],
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, stdout=subprocess.DEVNULL)
    return proc, port


@pytest.fixture
def standin(tmp_path):
    """An executor with only a stand-in phone (numpy + cloudpickle, no PyYAML)."""
    if not SIM_PY.exists():
        pytest.skip("create the phone stand-in env: uv venv .phone-sim && uv pip install --python "
                    ".phone-sim/bin/python numpy cloudpickle")
    pytest.importorskip("yaml")
    from pocketnode.sources import Address
    procs = []
    proc, port = _start_standin(tmp_path)
    procs.append(proc)
    ex = pocketnode.PocketExecutor(notes=False, auto_add=False)
    src = Address(f"127.0.0.1:{port}", token="t0ken", retry=0.5)
    ex.add_source(src)
    ex.standin = (tmp_path, port, procs)
    try:
        assert ex.wait_for("phone", 20)
        yield ex
    finally:
        src.stop()
        ex.shutdown(wait=False)
        for p in procs:
            p.kill()


def _ensure_phone(ex):
    """If the stand-in exited for a restart, start it again as the app would relaunch."""
    tmp_path, port, procs = ex.standin
    if procs[-1].poll() is not None:
        procs.append(_start_standin(tmp_path, port)[0])
    assert ex.wait_for("phone", 20)


def _uses_yaml():
    import yaml
    return yaml.__version__


def _yaml_size_mb() -> float:
    import importlib.metadata as md
    d = md.distribution("PyYAML")
    return sum(Path(d.locate_file(f)).stat().st_size for f in d.files
               if Path(d.locate_file(f)).is_file()) / 2**20


# ---------------------------------------------------------------- estimates


def test_hot_estimate_uses_measured_rate():
    mb = _yaml_size_mb()
    est = hot.estimate(["PyYAML"])
    assert set(est) == {"seconds", "mb", "measured"}
    assert est["measured"] is False
    assert abs(est["mb"] - mb) < 0.1 and est["seconds"] >= 5

    def with_rate(rate):
        app_mod.HISTORY.write_text("".join(
            json.dumps({"event": "hot", "packages": ["x"], "seconds": rate * m, "mb": m}) + "\n"
            for m in (10.0, 20.0)))
        return hot.estimate(["PyYAML"])

    slow, fast = with_rate(1000.0), with_rate(100.0)
    assert slow["measured"] and fast["measured"]
    # 3 s of overhead plus size x the measured seconds per MB (sizes from RECORD, within 1%).
    assert abs(slow["seconds"] - (3 + mb * 1000.0)) <= 0.01 * mb * 1000.0 + 1
    assert abs(fast["seconds"] - (3 + mb * 100.0)) <= 0.01 * mb * 100.0 + 1


def test_hot_estimate_skips_what_the_phone_has():
    import importlib.metadata as md
    have = {"numpy": md.version("numpy")}
    assert hot.estimate(["numpy"], have=have)["mb"] == 0
    assert hot.estimate(["numpy"])["mb"] > 1


# -------------------------------------------------------------------- misses


def test_misses_accumulate_and_expire():
    packages.record_miss("PyYAML")
    packages.record_miss("PyYAML")
    packages.record_miss("lightgbm")
    assert packages.recent_misses() == {"pyyaml": 2, "lightgbm": 1}
    d = json.loads(packages.MISSES.read_text())
    d["lightgbm"]["last"] = time.time() - 20 * 86400
    packages.MISSES.write_text(json.dumps(d))
    assert packages.recent_misses() == {"pyyaml": 2}
    assert packages.recent_misses(days=30) == {"pyyaml": 2, "lightgbm": 1}


# --------------------------------------------------------------------- table


def test_table_kinds():
    rows = {r.name.lower(): r for r in packages.table(check=False)}
    assert rows["numpy"].kind == "compiled"
    assert rows["cloudpickle"].kind == "pure" and rows["cloudpickle"].status == "auto"


def test_table_status_from_the_phone(standin):
    rows = {r.name.lower(): r for r in packages.table(check=False, executor=standin)}
    assert rows["numpy"].status == "on phone"
    assert rows["pyyaml"].status == "not on phone"


# ------------------------------------------------------------- add / remove


def test_add_and_remove_on_running_phone(standin, capsys):
    ex = standin
    with pytest.raises(PlacementError, match="PyYAML"):
        ex.on("phone").submit(_uses_yaml).result(timeout=30)

    r = packages.packages.add("PyYAML", executor=ex)
    out = capsys.readouterr().out
    assert "PyYAML" in r["added"] and not r["failed"], r
    assert "adding PyYAML: about" in out and "(estimate)" in out
    assert "added PyYAML" in out
    import yaml
    assert ex.on("phone").submit(_uses_yaml).result(timeout=30) == yaml.__version__

    r = packages.packages.remove("PyYAML", executor=ex)
    out = capsys.readouterr().out
    assert r["removed"] == ["PyYAML"]
    assert "removed PyYAML" in out
    _ensure_phone(ex)
    with pytest.raises(PlacementError, match="PyYAML"):
        ex.on("phone").submit(_uses_yaml).result(timeout=30)

    r = packages.packages.remove("numpy", executor=ex)
    out = capsys.readouterr().out
    assert r["removed"] == [] and r["bundled"] == ["numpy"]
    assert "numpy is part of the app build" in out and "removed" not in out

    packages.packages.remove("lightgbm", executor=ex)
    assert "lightgbm isn't on the phone" in capsys.readouterr().out


# -------------------------------------------------------------------- config


def test_always_mac_persists_once(capsys):
    packages.packages.always_mac("PyYAML")
    packages.packages.always_mac("PyYAML")
    assert app_mod.config()["always_mac"] == ["PyYAML"]
    assert json.loads((app_mod.CONFIG_DIR / "app.json").read_text())["always_mac"] == ["PyYAML"]
    assert "won't offer PyYAML again" in capsys.readouterr().out


# ----------------------------------------------------------------- checklist

CHECKLIST = r'''
import importlib.metadata as md, json, sys
from pocketnode.provision import packages as P
rows = [
    P.Row("numpy", md.version("numpy"), "compiled", "on phone", "ok", "", 30.0),
    P.Row("PyYAML", md.version("PyYAML"), "compiled", "not on phone", "ok", "", 1.0),
    P.Row("scipy", md.version("scipy"), "compiled", "not on phone", "shim", "", 90.0),
    P.Row("pandas", md.version("pandas"), "compiled", "not on phone", "blocked", "links AppKit", 60.0),
]
checked = {"numpy"}
chosen = P._checklist(rows, checked, {"pyyaml": 3}, False)
print("RESULT " + json.dumps({"chosen": None if chosen is None else sorted(chosen),
                              "checked": sorted(checked)}))
'''

DOWN = "\x1bOB"  # what xterm sends for the down arrow once curses turns on keypad mode


def _drive_checklist(tmp_path, keys: list[str]) -> dict:
    """Run the checklist on a real pseudo-terminal (30 x 120) and press `keys`.

    The terminal is opened here and handed to a subprocess rather than made with
    pexpect.spawn, which forks this (multi-threaded, after the stand-in tests)
    process and runs Python in the child before exec."""
    import fcntl
    import struct
    import termios
    pexpect = pytest.importorskip("pexpect")
    from pexpect import fdpexpect
    script = tmp_path / "checklist.py"
    script.write_text(CHECKLIST)
    env = {**os.environ, "TERM": "xterm-256color", "PYTHONPATH": str(ROOT / "src"),
           "POCKETNODE_HOME": str(tmp_path / "home"), "POCKETNODE_STAGE": str(tmp_path / "stage")}
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
    proc = subprocess.Popen([sys.executable, str(script)], stdin=slave, stdout=slave, stderr=slave,
                            env=env, start_new_session=True)
    os.close(slave)
    child = fdpexpect.fdspawn(master, encoding="utf-8", timeout=60)
    try:
        child.expect("space: toggle")
        child.expect("no changes")  # first frame fully drawn
        for k in keys:
            child.send(k)
            time.sleep(0.2)
        child.expect(r"RESULT (\{.*\})")
        res = json.loads(child.match.group(1))
        child.expect(pexpect.EOF)
        assert proc.wait(timeout=30) == 0, child.before
    finally:
        proc.kill()
        child.close()
    return res


def test_checklist_cancel(tmp_path):
    assert _drive_checklist(tmp_path, ["q"]) == {"chosen": None, "checked": ["numpy"]}


def test_checklist_toggle_second_row(tmp_path):
    # Order: checked first, then recent misses, blocked last: numpy, PyYAML, scipy, pandas.
    res = _drive_checklist(tmp_path, [DOWN, " ", "\r"])
    assert res == {"chosen": ["numpy", "pyyaml"], "checked": ["numpy"]}


def test_checklist_blocked_row_cannot_be_ticked(tmp_path):
    res = _drive_checklist(tmp_path, [DOWN, DOWN, DOWN, " ", "\r"])
    assert res == {"chosen": ["numpy"], "checked": ["numpy"]}


# -------------------------------------------------------------------- select


def test_select_applies_the_difference(standin, monkeypatch, capsys):
    ex = standin
    monkeypatch.setattr(packages, "_check_dist", lambda d: ("ok", ""))  # the static iOS check isn't under test
    seen = []

    def pick(chosen):
        def fake(rows, checked, misses, first, bundle=False):
            seen.append((set(checked), first))
            return set(chosen)
        monkeypatch.setattr(packages, "_checklist", fake)

    pick({"numpy", "pyyaml"})
    packages.packages.select(executor=ex)
    assert seen[-1] == ({"numpy"}, False)
    assert "added PyYAML" in capsys.readouterr().out
    phone = next(n for n in ex.nodes.values() if n.kind == "phone")
    assert "PyYAML" in phone.hello["packages"]

    pick({"numpy"})
    packages.packages.select(executor=ex)
    assert seen[-1][0] == {"numpy", "pyyaml"}
    assert "removed PyYAML" in capsys.readouterr().out
    _ensure_phone(ex)
    phone = next(n for n in ex.nodes.values() if n.kind == "phone")
    assert "PyYAML" not in phone.hello["packages"]

    packages.packages.select(executor=ex)
    assert "nothing to change" in capsys.readouterr().out
