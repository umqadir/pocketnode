"""Core behaviour against a local stand-in for the phone.

The simulated phone runs the real worker with phone guards, in its own Python
environment (.phone-sim: numpy + cloudpickle only), so routing, import
fetching and fallbacks behave as they do with the device.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

import pocketnode as pn
from pocketnode.errors import PlacementError

ROOT = Path(__file__).resolve().parents[1]
SIM_PY = ROOT / ".phone-sim" / "bin" / "python"


@pytest.fixture(scope="module")
def ex():
    if not SIM_PY.exists():
        pytest.skip("create the phone stand-in env: uv venv .phone-sim && uv pip install --python "
                    ".phone-sim/bin/python numpy cloudpickle")
    e = pn.connect(mac_workers=2, phone=False, simulate_phone=str(SIM_PY), notes=False)
    assert e.wait_for("phone", 30)
    assert e.wait_for("mac", 30)
    yield e
    e.shutdown(wait=False)


def phone(ex):
    return next(n for n in ex.nodes.values() if n.kind == "phone")


# ------------------------------------------------------------------- basics

def test_submit_lambda_and_closure(ex):
    k = 7
    assert ex.submit(lambda x: x * k, 6).result() == 42
    assert ex.on("phone").submit(lambda x: x * k, 6).result() == 42


def test_map_matches_builtin(ex):
    assert list(ex.map(lambda a, b: a + b, range(50), range(50))) == [a + b for a in range(50) for b in [a]]
    assert list(ex.map(abs, range(-20, 20), chunksize=7)) == [abs(i) for i in range(-20, 20)]


def test_numpy_roundtrip_exact(ex):
    x = np.random.default_rng(0).normal(size=(300, 300))
    r = ex.on("phone").submit(lambda a: a @ a.T, x).result()
    assert np.array_equal(r, x @ x.T)


def test_tasks_get_private_writable_copies(ex):
    x = np.zeros(200_000)

    def mutate(a):
        a += 1
        return float(a.sum())

    assert ex.on("phone").submit(mutate, x).result() == 200_000
    assert ex.on("phone").submit(mutate, x).result() == 200_000  # cache unaffected by the mutation
    assert x.sum() == 0


def test_buffer_crosses_once(ex):
    x = np.random.default_rng(1).normal(size=2_000_000)  # 16 MB
    node = phone(ex)
    before = node.bytes_sent
    futs = [ex.on("phone").submit(lambda a, i: float(a[i]), x, i) for i in range(20)]
    assert [f.result() for f in futs] == [float(x[i]) for i in range(20)]
    sent = node.bytes_sent - before
    assert sent < x.nbytes * 1.5, f"sent {sent} bytes for a {x.nbytes}-byte array used 20 times"


def test_stdout_forwarded(ex, capfd):
    ex.on("phone").submit(lambda: print("from the phone")).result()
    time.sleep(0.1)
    assert "from the phone" in capfd.readouterr().out


def test_user_exception_reraised_with_remote_traceback(ex):
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope") as ei:
        ex.on("mac").submit(boom).result()
    assert "boom" in str(ei.value.__cause__)


# ---------------------------------------------------------------- placement

def test_placement_overrides(ex):
    assert ex.on("mac").submit(os.getpid).placement.kind in ("", "mac")
    f = ex.on("phone").submit(lambda: 1)
    f.result()
    assert f.placement.kind == "phone"

    with pn.placement("phone"):
        f = ex.submit(lambda: 2)
    f.result()
    assert f.placement.kind == "phone"

    @pn.runs_on("mac")
    def g():
        return 3

    f = ex.submit(g)
    f.result()
    assert f.placement.kind == "mac"


def test_missing_compiled_module_auto_falls_back(ex):
    def uses_pandas():
        import pandas as pd
        return int(pd.Series([1, 2, 3]).sum())

    f = ex.submit(uses_pandas)
    assert f.result() == 6
    assert f.placement.kind == "mac"


def test_missing_compiled_module_forced_raises(ex):
    def uses_pandas():
        import pandas as pd
        return int(pd.Series([1, 2, 3]).sum())

    with pytest.raises(PlacementError, match="pandas"):
        ex.on("phone").submit(uses_pandas).result()


def test_user_code_catching_missing_import_still_reroutes(ex):
    # The phone lacks pandas; code that would quietly take a fallback branch
    # there must not: it runs on the Mac, where pandas exists.
    def optional():
        try:
            import pandas  # noqa: F401
            return "pandas"
        except ImportError:
            return "fallback"

    f = ex.submit(optional)
    assert f.result() == "pandas"


def test_pure_module_fetched_from_mac(ex):
    def uses_joblib():
        import joblib  # pure Python, not installed in the phone env
        return joblib.__version__

    import joblib
    f = ex.on("phone").submit(uses_joblib)
    assert f.result() == joblib.__version__


def test_mac_file_reads_go_to_mac(ex, tmp_path):
    p = Path("/tmp") / f"pn-test-{os.getpid()}.txt"
    p.write_text("on the mac")
    try:
        f = ex.submit(lambda: open(p).read())
        assert f.result() == "on the mac"
        assert f.placement.kind == "mac"
        with pytest.raises(PlacementError, match="Mac"):
            ex.on("phone").submit(lambda: open(p).read()).result()
    finally:
        p.unlink()


def test_processes_go_to_mac(ex):
    def spawn():
        import subprocess
        return subprocess.run([sys.executable, "-c", "print(5)"], capture_output=True, text=True).stdout.strip()

    with pytest.raises(PlacementError, match="process"):
        ex.on("phone").submit(spawn).result()


def test_phone_error_confirmed_on_mac(ex):
    # An error only the phone produces must never surface: the Mac re-runs it.
    def platform_dependent():
        import sys
        if sys.prefix.endswith(".phone-sim"):
            raise MemoryError("phone ran out")
        return "ok"

    with pn.placement("auto"):
        futs = [ex.submit(platform_dependent) for _ in range(12)]
    assert all(f.result() == "ok" for f in futs)


# ------------------------------------------------------------ resilience

def test_hot_phone_gets_no_new_work(ex):
    node = phone(ex)
    node.conn.send({"t": "override_state", "state": {"thermal": "critical"}})
    try:
        deadline = time.time() + 5
        while node.effective_slots() and time.time() < deadline:
            time.sleep(0.05)
        assert node.effective_slots() == 0
        futs = [ex.submit(lambda i: i, i) for i in range(10)]
        assert [f.result() for f in futs] == list(range(10))
        assert all(f.placement.kind == "mac" for f in futs)
    finally:
        node.conn.send({"t": "override_state", "state": {}})
        time.sleep(0.3)


def test_speculation_rescues_stalled_phone(ex):
    # A task stuck on a phone that went to the background finishes on the Mac.
    node = phone(ex)

    def slow_on_phone():
        import sys
        import time
        if sys.prefix.endswith(".phone-sim"):
            time.sleep(30)
        return "done"

    f = ex.on("phone").submit(lambda: 0)
    f.result()
    g = ex.submit(slow_on_phone)
    deadline = time.time() + 5
    while not any(a.task.future is g for a in node.running.values()) and time.time() < deadline:
        time.sleep(0.05)
    node.conn.send({"t": "override_state", "state": {"lifecycle": "background"}})
    try:
        assert g.result(timeout=20) == "done"
        assert g.placement.kind == "mac"
    finally:
        node.conn.send({"t": "override_state", "state": {}})
        time.sleep(0.3)


def test_phone_killed_mid_task_requeues(ex):
    src = next(s for s in ex.sources if s.describe() == "simulated phone")

    def slow():
        import time
        time.sleep(3)
        return "survived"

    f = ex.on("phone").submit(slow)
    time.sleep(1.0)
    for p in src.procs:
        p.kill()
    assert f.result(timeout=60) == "survived"  # re-run once the phone comes back
    assert ex.wait_for("phone", 30)


# ------------------------------------------------------------ integrations

def test_joblib_backend_matches_sequential(ex):
    joblib = pytest.importorskip("joblib")
    from joblib import Parallel, delayed

    X = np.random.default_rng(2).normal(size=(500, 50))
    ref = [float(np.linalg.norm(X[i:i + 10])) for i in range(0, 500, 10)]
    with pn.parallel(ex):
        got = Parallel(n_jobs=-1)(delayed(lambda a, i: float(np.linalg.norm(a[i:i + 10])))(X, i)
                                  for i in range(0, 500, 10))
    assert got == ref


def test_joblib_large_arrays_read_only_like_joblib(ex):
    pytest.importorskip("joblib")
    from joblib import Parallel, delayed

    big = np.ones(300_000)  # 2.4 MB > joblib's 1 MB memmap threshold
    with pn.parallel(ex):
        flags = Parallel(n_jobs=2)(delayed(lambda a: a.flags.writeable)(big) for _ in range(2))
    assert flags == [False, False]


def test_pandas_accessor_equals_pandas(ex):
    pd = pytest.importorskip("pandas")
    rng = np.random.default_rng(3)
    df = pd.DataFrame({"g": rng.integers(0, 20, 3000), "x": rng.normal(size=3000)})
    f = lambda d: d.x.abs().sum()  # noqa: E731
    assert df.pn.groupby("g", executor=ex).apply(f).equals(df.groupby("g").apply(f))
    assert df.x.pn.map(lambda v: v * 3, executor=ex).equals(df.x.map(lambda v: v * 3))


def test_results_held_across_disconnect(tmp_path):
    """A result finished while the link is down is delivered on reconnect."""
    import socket
    import subprocess

    tok = tmp_path / "token"
    tok.write_text("t0ken")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen([str(SIM_PY), "-m", "pocketnode.worker", "--listen", f"127.0.0.1:{port}",
                             "--token-file", str(tok), "--phone", "--kind", "phone", "--name", "held-test",
                             "--cache-dir", str(tmp_path / "cache")],
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, stdout=subprocess.DEVNULL)
    from pocketnode.sources import Address
    e = pn.PocketExecutor(notes=False)
    src = Address(f"127.0.0.1:{port}", token="t0ken", retry=0.5)
    e.add_source(src)
    try:
        assert e.wait_for("phone", 20)

        def slow():
            import time
            time.sleep(2)
            return "held"

        f = e.on("phone").submit(slow)
        time.sleep(0.5)
        next(iter(e.nodes.values())).conn.close()  # link drops mid-task
        assert f.result(timeout=30) == "held"
        assert len(f.placement.attempts) == 1, "delivered the held result, not a re-run"
    finally:
        src.stop()
        e.shutdown(wait=False)
        proc.kill()
