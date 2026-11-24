"""Progress lines: in-place redraws on a terminal, rate-limited lines elsewhere, quiet mode,
failures, threads; the xcodebuild/devicectl output parsers; and the stager's counts."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import threading

import pytest

os.environ.setdefault("POCKETNODE_HOME", tempfile.mkdtemp(prefix="pn-test-home-"))

from pocketnode import progress  # noqa: E402
from pocketnode.provision import app as app_mod  # noqa: E402
from pocketnode.provision import stage as stage_mod  # noqa: E402


class TTY(io.StringIO):
    def isatty(self):
        return True


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(progress, "_clock", c)
    monkeypatch.setattr(progress, "TICK_EVERY", 3600)  # no ticks unless a test asks for them
    return c


def _at(clock, seconds):
    clock.t = 1000.0 + seconds


def test_elapsed_format():
    assert progress.elapsed(0) == "0:00"
    assert progress.elapsed(102.9) == "1:42"
    assert progress.elapsed(291) == "4:51"
    assert progress.elapsed(3725) == "1:02:05"


def test_terminal_redraws_in_place(clock):
    tty = TTY()
    with progress.Step("Building the app", stream=tty) as p:
        _at(clock, 30)
        p.detail("compiling sources")
        _at(clock, 102)
        p.update(212, 328, detail="signing frameworks")
        _at(clock, 291)
    out = tty.getvalue()
    assert "\rBuilding the app  0:00\x1b[K" in out
    assert "\rBuilding the app  compiling sources  0:30\x1b[K" in out
    assert "\rBuilding the app  signing frameworks 212/328  1:42\x1b[K" in out
    assert out.endswith("\rBuilding the app  done in 4:51\x1b[K\n")
    assert out.count("\n") == 1  # one line, redrawn; the final one ends it


def test_terminal_redraws_at_most_five_times_a_second(clock):
    tty = TTY()
    with progress.Step("Staging packages", stream=tty) as p:
        p.update(1, 10, detail="retagging")  # a new detail draws at once
        p.update(2, 10)  # same instant: skipped
        _at(clock, 0.3)
        p.update(3, 10)
    out = tty.getvalue()
    assert "retagging 1/10" in out and "retagging 2/10" not in out and "retagging 3/10" in out


def test_terminal_ticks_while_nothing_reports(clock, monkeypatch):
    monkeypatch.setattr(progress, "TICK_EVERY", 0.01)
    tty = TTY()
    done = threading.Event()
    with progress.Step("Waiting", stream=tty):
        _at(clock, 65)
        for _ in range(200):
            if "1:05" in tty.getvalue():
                done.set()
                break
            threading.Event().wait(0.01)
    assert done.is_set()


def test_terminal_truncates_long_lines(clock):
    tty = TTY()  # no real terminal behind it: 80 columns
    with progress.Step("Adding " + ", ".join(f"package{i}" for i in range(40)), stream=tty) as p:
        p.update(3, 9, detail="sending", unit="MB")
    for part in tty.getvalue().split("\r"):
        assert len(part.replace("\x1b[K", "").rstrip("\n")) <= 79


def test_counts_with_units(clock):
    tty = TTY()
    with progress.Step("Adding torch to iPhone", stream=tty) as p:
        p.update(37.4, 150.2, detail="sending", unit="MB")
        _at(clock, 1)
        p.update(45, 100, detail="installing", unit="%")
        _at(clock, 2)
        p.update(2.25, 5.5, detail="downloading", unit="MB")
    out = tty.getvalue()
    assert "sending 37/150 MB" in out and "installing 45%" in out and "downloading 2.2/5.5 MB" in out


def test_log_mode_prints_on_detail_changes_and_every_15_s(clock):
    log = io.StringIO()
    with progress.Step("Building the app", stream=log) as p:
        _at(clock, 1)
        p.update(1, 10, detail="copying packages")
        _at(clock, 2)
        p.update(2, 10)  # same detail, soon after: nothing
        _at(clock, 17)
        p.update(3, 10)  # 15 s since the last line
        _at(clock, 18)
        p.detail("retagging")
    assert "\r" not in log.getvalue()
    assert log.getvalue().splitlines() == [
        "Building the app",
        "Building the app: copying packages 1/10 (0:01)",
        "Building the app: copying packages 3/10 (0:17)",
        "Building the app: retagging (0:18)",
        "Building the app: done in 0:18",
    ]


def test_jupyter_uses_log_mode(clock, monkeypatch):
    monkeypatch.setitem(sys.modules, "ipykernel", object())
    tty = TTY()
    with progress.Step("Building the app", stream=tty) as p:
        p.detail("compiling sources")
    assert "\r" not in tty.getvalue()
    assert tty.getvalue().splitlines()[-1] == "Building the app: done in 0:00"


def test_quiet_prints_nothing(clock, monkeypatch):
    tty = TTY()
    said = []
    with progress.step("Building the app", stream=tty) as p:  # POCKETNODE_QUIET=1 in the test run
        p.update(1, 2, detail="x")
        p.echo("still said", said.append)
    monkeypatch.delenv("POCKETNODE_QUIET")
    with progress.step("Building the app", stream=tty, quiet=True) as p:
        p.update(1, 2, detail="x")
    progress.quiet(True)
    try:
        with progress.step("Building the app", stream=tty) as p:
            p.update(1, 2, detail="x")
    finally:
        progress.quiet(False)
    assert tty.getvalue() == "" and said == ["still said"]
    with progress.step("Building the app", stream=tty):
        pass
    assert "done in" in tty.getvalue()


def test_failure_says_so_and_reraises(clock):
    tty, log = TTY(), io.StringIO()
    with pytest.raises(ValueError, match="boom"):
        with progress.Step("Building the app", stream=tty):
            _at(clock, 12)
            raise ValueError("boom")
    assert tty.getvalue().endswith("\rBuilding the app  failed after 0:12\x1b[K\n")
    with pytest.raises(KeyboardInterrupt):
        with progress.Step("Building the app", stream=log):
            raise KeyboardInterrupt
    assert log.getvalue().splitlines()[-1].startswith("Building the app: stopped after")


def test_echo_clears_the_line_first(clock):
    tty = TTY()
    with progress.Step("  waiting for Developer Mode", stream=tty) as p:
        p.echo("  now: the phone is locked", lambda s: tty.write(s + "\n"))
    out = tty.getvalue()
    i = out.index("  now: the phone is locked\n")
    assert out[:i].endswith("\r\x1b[K")
    assert out.endswith("\r  waiting for Developer Mode  done in 0:00\x1b[K\n")


def test_a_broken_stream_never_raises(clock):
    class Broken(TTY):
        def write(self, s):
            raise OSError("gone")
    with progress.Step("Building the app", stream=Broken()) as p:
        p.update(1, 2, detail="x")


def test_threads(clock):
    for stream in (TTY(), io.StringIO()):
        p = progress.Step("Retagging", stream=stream).start()
        tick = progress.counter(p, 8 * 300, "retagging")
        seen = []

        class Watch:
            def update(self, done=None, total=None, detail=None, unit=None):
                seen.append(done)

        tick2 = progress.counter(Watch(), 8 * 300, "retagging")

        def work():
            for _ in range(300):
                tick()
                tick2()
        ts = [threading.Thread(target=work) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        p.finish()
        assert seen == sorted(seen) and seen[-1] == 2400  # counts arrive in order, to the end
        assert stream.getvalue().count("done in") == 1


# ------------------------------------------------------------------ parsers

BUILD_LINES = """\
PrecompileModule /tmp/build/SDKExplicitPrecompiledModules/Foundation-ABC.scan
CompileC /tmp/build/Objects-normal/arm64/pn_core.o /tmp/ios/App/pn_core.c normal arm64 c com.apple.compilers.llvm.clang.1_0.compiler (in target 'PocketNode' from project 'PocketNode')
    cd /tmp/ios
Ld /tmp/build/Products/Debug-iphonesimulator/PocketNode.app/PocketNode normal (in target 'PocketNode' from project 'PocketNode')
PhaseScriptExecution Install\\ Python,\\ packages\\ and\\ token /tmp/build/Script-2D64.sh (in target 'PocketNode' from project 'PocketNode')
POCKETNODE_PROGRESS phase copying-python
Installing binary for lib-dynload/_bz2.cpython-313-iphonesimulator.so
POCKETNODE_PROGRESS phase copying-packages
POCKETNODE_PROGRESS frameworks 1 328
POCKETNODE_PROGRESS sign 212 328
CodeSign /tmp/build/Products/Debug-iphonesimulator/PocketNode.app (in target 'PocketNode' from project 'PocketNode')
** BUILD SUCCEEDED **
"""


def test_build_output_parser():
    got = [app_mod.build_progress(l) for l in BUILD_LINES.splitlines()]
    assert got == [
        ("compiling sources", None, None),
        ("compiling sources", None, None),
        None,
        ("compiling sources", None, None),
        ("installing Python and packages", None, None),
        ("copying Python", None, None),
        None,
        ("copying packages", None, None),
        ("copying frameworks", 1, 328),
        ("signing frameworks", 212, 328),
        ("signing the app", None, None),
        None,
    ]


def test_install_percent():
    assert app_mod.install_percent("Installing: 45%") == 45
    assert app_mod.install_percent("13:01:02  Copying 12.5 % complete") == 12.5
    assert app_mod.install_percent("13:01:02  Acquired tunnel connection to device.") is None
    assert app_mod.install_percent("500% nonsense") is None


def test_run_streaming_hands_over_lines_and_keeps_the_log(tmp_path):
    lines = []
    log = tmp_path / "build.log"
    r = app_mod._run_streaming(["/bin/sh", "-c", "echo one; echo err >&2; echo two; exit 3"], lines.append, log=log)
    assert lines == ["one\n", "two\n"]
    assert r.returncode == 3 and r.stdout == "one\ntwo\n" and r.stderr == "err\n"
    assert log.read_text() == "one\ntwo\nerr\n"  # as before: standard output, then errors


def test_follow_reads_markers_as_they_are_written(tmp_path):
    f = tmp_path / "iphonesimulator.progress"  # doesn't exist yet: the script creates it
    got, stop = [], threading.Event()
    t = threading.Thread(target=app_mod._follow, args=(f, got.append, stop, 0.01))
    t.start()
    with open(f, "a") as w:
        w.write("POCKETNODE_PROGRESS sign 1 2\nPOCKETNODE_PROGRESS si")
    for _ in range(300):
        if got:
            break
        threading.Event().wait(0.01)
    assert got == ["POCKETNODE_PROGRESS sign 1 2"]  # the half-written line waits
    with open(f, "a") as w:
        w.write("gn 2 2\n")
    stop.set()
    t.join()
    assert got == ["POCKETNODE_PROGRESS sign 1 2", "POCKETNODE_PROGRESS sign 2 2"]


# ---------------------------------------------------------------- the stager

class Recorder:
    def __init__(self):
        self.calls = []

    def update(self, done=None, total=None, detail=None, unit=None):
        self.calls.append((detail, done, total))

    def detail(self, text):
        self.update(None, None, detail=text)


def test_stager_reports_increasing_counts(tmp_path):
    """Stage PyYAML (one compiled module) in place for the simulator and watch the reports."""
    rec = Recorder()
    rep = stage_mod.Stager("iphonesimulator", tmp_path / "out", inplace=True).stage(["PyYAML"], progress=rec)
    assert "PyYAML" in rep.dists and rep.binaries == 1
    by_phase: dict[str, list] = {}
    for detail, done, total in rec.calls:
        by_phase.setdefault(detail, []).append((done, total))
    for phase in ("copying packages", "reading binaries", "checking against the iOS SDK", "retagging"):
        counts = [d for d, _ in by_phase[phase]]
        assert counts == sorted(counts) and counts[0] == 0, phase
        assert by_phase[phase][-1][0] == by_phase[phase][-1][1], phase
    assert by_phase["retagging"][-1] == (1, 1)
    assert by_phase["copying packages"][-1] == (1, 1)
