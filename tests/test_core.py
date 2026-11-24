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


def _result_or_dump(ex, fut, timeout=20):
    """fut.result(), but on a hang show the scheduler's view before failing."""
    import concurrent.futures
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        lines = [ex.describe()]
        for n in ex.nodes.values():
            lines.append(f"{n.name}: state={n.state} running={[(a.task.fn_label, round(time.monotonic() - a.started, 1)) for a in n.running.values()]}")
        lines.append(f"queue={[(t.fn_label, t.queued, t.done, t.placement) for t in ex.queue]}")
        lines.append(f"tasks={[(t.fn_label, t.queued, t.done, t.placement, list(t.excluded)) for t in ex.tasks.values()]}")
        raise AssertionError("\n".join(lines))


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
    _result_or_dump(ex, f)
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


def test_first_phone_result_verified_against_mac(ex):
    # Same answer everywhere: the phone's results are trusted after one check.
    futs = [ex.submit(lambda x: (np.arange(1000.0) * x).sum(), i) for i in range(30)]
    assert [f.result() for f in futs] == [float((np.arange(1000.0) * i).sum()) for i in range(30)]


def test_phone_result_that_differs_is_replaced_by_macs(ex):
    def answer():
        import sys
        return 41.0 if sys.prefix.endswith(".phone-sim") else 42.0

    futs = [ex.submit(answer) for _ in range(12)]
    assert all(f.result() == 42.0 for f in futs)


def test_nondeterministic_function_still_uses_phone(ex):
    def noisy():
        import random
        return random.random()

    futs = [ex.submit(noisy) for _ in range(20)]
    assert all(0 <= f.result() < 1 for f in futs)


def test_verification_ignores_fields_that_vary_but_checks_the_rest(ex):
    # Like scikit-learn's _fit_and_score: timings differ on every run, scores must not.
    def timed_ok(i):
        import time
        return {"fit_time": time.perf_counter(), "score": i * 2.0}

    def timed_wrong(i):
        import sys
        import time
        return {"fit_time": time.perf_counter(), "score": 41.0 if sys.prefix.endswith(".phone-sim") else 42.0}

    futs = [ex.submit(timed_ok, i) for i in range(20)]
    assert [f.result()["score"] for f in futs] == [i * 2.0 for i in range(20)]
    assert any(f.placement.kind == "phone" for f in futs), "a function with a timing field still uses the phone"
    futs = [ex.submit(timed_wrong, i) for i in range(12)]
    assert all(f.result()["score"] == 42.0 for f in futs)


def test_batches_are_identified_by_the_function_inside():
    pytest.importorskip("joblib")
    from joblib import delayed
    from joblib.parallel import BatchedCalls
    from pocketnode.executor import _fn_identity, _run_chunk, _unwrap

    def f(x):
        return x

    def g(x):
        return x + 1

    def key(batch):
        inner, weight, args = _unwrap(batch, ())
        return _fn_identity(inner, args)[0], weight

    bf = BatchedCalls([delayed(f)(1), delayed(f)(2)], ("threading", 1))
    bg = BatchedCalls([delayed(g)(1)], ("threading", 1))
    assert key(bf)[1] == 2 and key(bg)[1] == 1
    assert key(bf)[0] != key(bg)[0]
    inner, weight, args = _unwrap(_run_chunk, (f, [(1,), (2,), (3,)]))
    assert inner is f and weight == 3 and args == (1,)
    assert _fn_identity(f, (1,))[0] != _fn_identity(f, ("a",))[0]


def test_cancelled_duplicates_leave_every_phone_slot_working(ex):
    # Tasks the Mac finishes first are cancelled on the phone mid-run. The
    # cancel must never land outside the task it was meant for.
    node = phone(ex)

    def spin_on_phone(i):
        import sys
        import time
        if sys.prefix.endswith(".phone-sim"):
            t = time.time()
            while time.time() - t < 20:
                sum(range(2000))
        return i

    for _ in range(3):
        futs = [ex.submit(spin_on_phone, i) for i in range(10)]
        assert [f.result(timeout=30) for f in futs] == list(range(10))

    def nap():
        import time
        time.sleep(1.0)
        return 1

    deadline = time.time() + 10
    while node.running and time.time() < deadline:
        time.sleep(0.05)
    t0 = time.time()
    futs = [ex.on("phone").submit(nap) for _ in range(node.slots)]
    assert sum(f.result(timeout=30) for f in futs) == node.slots
    assert time.time() - t0 < 4.0, "all phone slots ran at once"


def test_c_level_relative_write_goes_to_mac(ex):
    # A relative path written from C (pyarrow, HDF5, ...) bypasses the Python
    # guards. The phone's working directory refuses it, so the Mac runs it.
    def write_from_c():
        import ctypes
        import os
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.open(b"pn-c-write.bin", os.O_CREAT | os.O_WRONLY, 0o644)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "open failed")
        os.close(fd)
        return "written"

    try:
        f = ex.submit(write_from_c)
        assert f.result(timeout=30) == "written"
        assert f.placement.kind == "mac"
        assert Path("pn-c-write.bin").exists(), "the file is on the Mac, where the user asked for it"
    finally:
        Path("pn-c-write.bin").unlink(missing_ok=True)


def _standalone_phone(tmp_path, *extra):
    import socket
    import subprocess
    tok = tmp_path / "token"
    tok.write_text("t0ken")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen([str(SIM_PY), "-m", "pocketnode.worker", "--listen", f"127.0.0.1:{port}",
                             "--token-file", str(tok), "--phone", "--kind", "phone", "--name", "standalone",
                             "--cache-dir", str(tmp_path / "cache"), *extra],
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, stdout=subprocess.DEVNULL)
    return proc, port


def test_two_sessions_share_the_phone(tmp_path):
    from pocketnode.sources import Address
    proc, port = _standalone_phone(tmp_path)
    a, b = pn.PocketExecutor(notes=False), pn.PocketExecutor(notes=False)
    srcs = []
    try:
        for e in (a, b):
            srcs.append(Address(f"127.0.0.1:{port}", token="t0ken", retry=0.5))
            e.add_source(srcs[-1])
        assert a.wait_for("phone", 20) and b.wait_for("phone", 20)

        def work(tag, i):
            import time
            time.sleep(0.05)
            return tag, i

        fa = [a.on("phone").submit(work, "a", i) for i in range(20)]
        fb = [b.on("phone").submit(work, "b", i) for i in range(20)]
        assert [f.result(timeout=30) for f in fa] == [("a", i) for i in range(20)]
        assert [f.result(timeout=30) for f in fb] == [("b", i) for i in range(20)]
        assert a.nodes and b.nodes, "neither session was kicked off"
    finally:
        for s in srcs:
            s.stop()
        a.shutdown(wait=False)
        b.shutdown(wait=False)
        proc.kill()


def test_input_bigger_than_the_phone_cache(tmp_path):
    from pocketnode.sources import Address
    proc, port = _standalone_phone(tmp_path, "--store-budget", str(1 << 20))
    e = pn.PocketExecutor(notes=False)
    src = Address(f"127.0.0.1:{port}", token="t0ken", retry=0.5)
    e.add_source(src)
    try:
        assert e.wait_for("phone", 20)
        x = np.arange(1_000_000, dtype="float64")  # 8 MB against a 1 MB cache
        futs = [e.on("phone").submit(lambda a, i: float(a[i]), x, i) for i in range(4)]
        assert [f.result(timeout=30) for f in futs] == [0.0, 1.0, 2.0, 3.0]
    finally:
        src.stop()
        e.shutdown(wait=False)
        proc.kill()


def test_large_buffers_hash_the_same_in_pieces():
    from pocketnode.wire import digest
    x = np.random.default_rng(5).integers(0, 255, 20_000_003, dtype="uint8")
    assert digest(x) == digest(bytes(x)) == digest(memoryview(x))
    y = x.copy()
    y[12_345_678] ^= 1
    assert digest(x) != digest(y)


def test_pure_means_no_compiled_code_underneath():
    from pocketnode import envinfo, router
    assert router.is_pure({"json", "joblib"})
    assert not router.is_pure({"numpy"})
    assert envinfo.needs_native("pandas")
    assert not envinfo.needs_native("joblib")
    if "dask" in envinfo.manifest():  # pure Python itself, built on compiled packages
        assert envinfo.manifest()["dask"]["kind"] == "pure" and not router.is_pure({"dask"})


def test_task_that_kills_the_phone_app(ex):
    def dies_on_phone():
        import os
        import sys
        if sys.prefix.endswith(".phone-sim"):
            os._exit(3)
        return "fine"

    # Automatic placement: the Mac runs it instead.
    assert ex.submit(dies_on_phone).result(timeout=60) == "fine"
    assert ex.wait_for("phone", 30)
    # Forced onto the phone: an error after the second crash, not an endless loop.
    with pytest.raises(PlacementError, match="twice"):
        ex.on("phone").submit(dies_on_phone).result(timeout=90)
    assert ex.wait_for("phone", 30)
    assert ex.on("phone").submit(lambda: 7).result(timeout=30) == 7
