"""Release acceptance check: every user-facing feature, against the connected phone.

    pocketnode check [--address HOST:PORT] [--wait SECONDS] [--quick]

Prints one line per item as it finishes (PASS, FAIL or SKIP), then a summary.
Phone results are compared with the same function run on this Mac. An item is
skipped when a package it needs is missing on the Mac, or on the phone when the
item runs there.
"""

from __future__ import annotations

import importlib
import math
import sys
import threading
import time
import uuid
from pathlib import Path

import cloudpickle

import pocketnode as pn

T = 120  # seconds allowed for any one result
CHECK_TIMEOUT = 600  # seconds allowed for any one item

# The tasks below travel by value, so a phone app built before this module existed runs them.
cloudpickle.register_pickle_by_value(sys.modules[__name__])


class Skip(Exception):
    pass


# ------------------------------------------------------------------ helpers

def _phone(ex):
    ex.wait_for("phone", 30)
    n = next((n for n in list(ex.nodes.values()) if n.kind == "phone"), None)
    if n is None:
        raise RuntimeError("no phone connected")
    return n


def _need(ex, *mods, phone=True):
    """Import each module on the Mac; with phone=True its distribution must be on the phone too."""
    from . import envinfo, router
    for m in mods:
        try:
            importlib.import_module(m)
        except ImportError:
            raise Skip(f"{m} isn't installed on this Mac") from None
        info = envinfo.manifest().get(m, {})
        dist = info.get("dist", m)
        if not phone:
            continue
        node = _phone(ex)
        have = node.packages.get(router._norm(dist))
        if have is None:
            raise Skip(f"{dist} isn't on the phone")
        mac = info.get("version")
        if mac and info.get("kind") == "compiled" and \
                not router.same_version(have, mac, router.policy_for(node, ex.version_policy)):
            raise Skip(f"{dist} is {have} on the phone but {mac} on this Mac")


def _expect(cond, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _close(a, b, tol=1e-6) -> bool:
    if isinstance(a, dict):
        return isinstance(b, dict) and a.keys() == b.keys() and all(_close(a[k], b[k], tol) for k in a)
    if isinstance(a, (list, tuple)):
        return isinstance(b, (list, tuple)) and len(a) == len(b) and all(_close(x, y, tol) for x, y in zip(a, b))
    if hasattr(a, "shape") and hasattr(a, "dtype"):
        import numpy as np
        if np.shape(a) != np.shape(b):
            return False
        return bool(np.allclose(a, b, rtol=tol, atol=tol) if a.dtype.kind in "fc" else np.array_equal(a, b))
    if isinstance(a, float) or isinstance(b, float):
        return math.isclose(a, b, rel_tol=tol, abs_tol=tol)
    return a == b


def _short(v) -> str:
    s = repr(v)
    return s if len(s) < 80 else s[:77] + "..."


def _phone_vs_mac(ex, fn, *args, tol=1e-6) -> str:
    t0 = time.perf_counter()
    got = ex.on("phone").submit(fn, *args).result(timeout=T)
    dt = time.perf_counter() - t0
    want = fn(*args)
    _expect(_close(got, want, tol), f"phone {_short(got)} != Mac {_short(want)}")
    return f"matches the Mac ({dt:.2f} s on the phone)"


def _raises_placement(fut, what: str) -> None:
    try:
        fut.result(timeout=T)
    except pn.PlacementError:
        return
    raise AssertionError(f"{what} forced to the phone returned instead of raising PlacementError")


def _ran(ex, n0: int) -> str:
    kinds = [k for _, _, k, _ in list(ex.history)[n0:]]
    return f"phone {kinds.count('phone')}, Mac {kinds.count('mac')} tasks"


# ------------------------------------------------- packages: phone vs Mac

def _t_linalg(n):
    import numpy as np
    a = np.random.default_rng(0).normal(size=(n, n))
    return {"sv": np.linalg.svd(a, compute_uv=False), "solve": np.linalg.solve(a, a.sum(1)),
            "inv_trace": float(np.trace(np.linalg.inv(a))), "eigvalsh": np.linalg.eigvalsh(a @ a.T)}


def _t_groupby(n):
    import numpy as np
    import pandas as pd
    df = pd.DataFrame({"g": np.arange(n) % 7, "v": np.arange(n, dtype=float) ** 0.5})
    return df.groupby("g").agg(s=("v", "sum"), m=("v", "mean"), n=("v", "size")).to_dict()


def _t_optimize():
    from scipy.optimize import minimize, rosen, rosen_der
    r = minimize(rosen, [1.3, 0.7, 0.8, 1.9, 1.2], jac=rosen_der, method="BFGS")
    return r.x, int(r.nit)


def _t_forest(n, trees):
    from sklearn.datasets import make_classification
    from sklearn.ensemble import RandomForestClassifier
    X, y = make_classification(n, 20, random_state=0)
    m = RandomForestClassifier(trees, n_jobs=-1, random_state=0).fit(X, y)
    return m.predict_proba(X[:100]), float(m.score(X, y))


def _t_parquet(n):
    import io
    import pyarrow as pa
    import pyarrow.parquet as pq
    t = pa.table({"i": list(range(n)), "x": [i / 4 for i in range(n)], "s": [str(i % 13) for i in range(n)]})
    buf = io.BytesIO()
    pq.write_table(t, buf)
    buf.seek(0)
    return pq.read_table(buf).to_pydict()


def _t_polars(n):
    import polars as pl
    df = pl.DataFrame({"g": [i % 5 for i in range(n)], "v": [float(i) for i in range(n)]})
    return (df.lazy().filter(pl.col("v") > 100).group_by("g")
            .agg(pl.col("v").sum().alias("s"), pl.len().alias("n")).sort("g").collect().to_dicts())


def _t_torch():
    import torch
    torch.manual_seed(0)
    m = torch.nn.Sequential(torch.nn.Linear(8, 16), torch.nn.Tanh(), torch.nn.Linear(16, 1))
    opt = torch.optim.Adam(m.parameters(), lr=0.01)
    X = torch.randn(256, 8)
    y = X.sum(1, keepdim=True)
    for _ in range(50):
        opt.zero_grad()
        loss = ((m(X) - y) ** 2).mean()
        loss.backward()
        opt.step()
    return float(loss), [p.detach().flatten().tolist() for p in m.parameters()]


def _t_torch_gpu():
    """The same short training run on the node's GPU and CPU; None if torch has no GPU there."""
    import torch
    if not torch.backends.mps.is_available():
        return None

    def run(dev):
        torch.manual_seed(0)
        m = torch.nn.Sequential(torch.nn.Linear(16, 32), torch.nn.GELU(), torch.nn.LayerNorm(32),
                                torch.nn.Linear(32, 1)).to(dev)
        X = torch.randn(256, 16, generator=torch.Generator().manual_seed(0)).to(dev)
        y = X.sum(1, keepdim=True)
        opt = torch.optim.Adam(m.parameters(), lr=0.01)
        for _ in range(40):
            opt.zero_grad()
            loss = ((m(X) - y) ** 2).mean()
            loss.backward()
            opt.step()
        return float(loss), float(torch.sin(X).std()), float((X @ X.T).trace())

    return run("cpu"), run("mps")


def torch_gpu(ex, quick):
    _need(ex, "torch")
    r = ex.on("phone").submit(_t_torch_gpu).result(timeout=T)
    if r is None:
        raise Skip("torch's Metal backend is off on the phone")
    _expect(_close(r[0], r[1], 1e-3), f"GPU {_short(r[1])} != CPU {_short(r[0])}")
    return "training on the phone's GPU matches its CPU"


def _t_forward(model, x):
    import torch
    with torch.no_grad():
        return model(x)


class _TorchServer:
    """A model kept on the phone, on its GPU when it has one."""

    def __init__(self, model):
        import torch
        self.dev = "mps" if torch.backends.mps.is_available() else "cpu"
        self.model = model.to(self.dev).eval()

    def where(self):
        return self.dev

    def predict(self, x):
        import torch
        with torch.no_grad():
            return self.model(x.to(self.dev)).cpu()


def torch_model_once(ex, quick):
    _need(ex, "torch")
    import torch
    model = torch.nn.Sequential(torch.nn.Linear(1024, 1024), torch.nn.ReLU(), torch.nn.Linear(1024, 1024)).eval()
    x = torch.randn(32, 1024)  # (fresh weights each run: never cached before)
    with torch.no_grad():
        want = model(x)
    size = sum(p.numel() * 4 for p in model.parameters())
    ph, node = ex.on("phone"), _phone(ex)
    b0 = node.bytes_sent
    y1 = ph.submit(_t_forward, model, x).result(timeout=T)
    b1 = node.bytes_sent
    y2 = ph.submit(_t_forward, model, x).result(timeout=T)
    b2 = node.bytes_sent
    _expect(torch.allclose(y1, want, atol=1e-4) and torch.allclose(y2, want, atol=1e-4), "wrong result")
    _expect(b2 - b1 < size / 20, f"the second task sent {(b2 - b1) / 2**20:.1f} MB of weights again")
    with ph.create(_TorchServer, model) as server:
        dev = server.where().result(timeout=T)
        b3 = node.bytes_sent
        ys = [server.predict(x).result(timeout=T) for _ in range(3)]
        b4 = node.bytes_sent
    _expect(all(torch.allclose(y, want, atol=1e-3) for y in ys), f"the model kept on the phone's {dev} differs")
    _expect(b4 - b3 < size / 20, f"calls on the kept model sent {(b4 - b3) / 2**20:.1f} MB")
    return (f"{size / 2**20:.0f} MB of weights: first task sent {(b1 - b0) / 2**20:.1f} MB, the second "
            f"{(b2 - b1) / 1024:.0f} KB; kept on the phone's {dev}, 3 calls match")


def _t_mlx(seed):
    import mlx.core as mx
    import numpy as np
    a = np.random.default_rng(seed).normal(size=(128, 128)).astype("float32")
    g = mx.array(a)
    out = mx.softmax(mx.matmul(g, g.T), axis=-1)
    mx.eval(out)
    return mx.metal.is_available(), np.array(out)


def mlx_gpu(ex, quick):
    _need(ex, "mlx")
    import numpy as np
    on_gpu, got = ex.on("phone").submit(_t_mlx, 0).result(timeout=T)
    a = np.random.default_rng(0).normal(size=(128, 128)).astype("float32")
    z = a @ a.T
    e = np.exp(z - z.max(-1, keepdims=True))
    _expect(on_gpu, "MLX reports no Metal GPU on the phone")
    _expect(_close(got, e / e.sum(-1, keepdims=True), 1e-3), "MLX on the phone's GPU differs from numpy")
    return "matmul and softmax on the phone's GPU match numpy"


def _library(module, task, small=(), big=(), tol=1e-6):
    def check(ex, quick):
        _need(ex, module)
        return _phone_vs_mac(ex, task, *(small if quick else big), tol=tol)
    return check


# ------------------------------------------------------------ pure Python

def _t_loop(n):  # stdlib only: runs in a phone subinterpreter where it has them
    s = 0
    for i in range(n):
        s += i * i % 7
    return s


def pure_task(ex, quick):
    on = "on" if _phone(ex).hello.get("subinterpreters") else "off"
    return f"{_phone_vs_mac(ex, _t_loop, 200_000)}; subinterpreters {on}"


def pure_parallel(ex, quick):
    n = 1_000_000 if quick else 3_000_000
    ph, slots = ex.on("phone"), _phone(ex).slots
    ph.submit(_t_loop, 10).result(timeout=T)  # warm up
    t0 = time.perf_counter()
    one = ph.submit(_t_loop, n).result(timeout=T)
    t1, t0 = time.perf_counter() - t0, time.perf_counter()
    many = [f.result(timeout=T) for f in [ph.submit(_t_loop, n) for _ in range(slots)]]
    tn = time.perf_counter() - t0
    _expect([one, *many] == [_t_loop(n)] * (slots + 1), "wrong result")
    return f"1 task {t1:.2f} s, {slots} tasks {tn:.2f} s: {slots * t1 / tn:.1f}x"


def pure_package_on_demand(ex, quick):
    from . import envinfo, router
    man, have = envinfo.manifest(), _phone(ex).packages
    for mod in ("tabulate", "joblib", "packaging", "six", "idna", "attr", "toolz", "decorator"):
        info = man.get(mod)
        if (info and info["kind"] == "pure" and not envinfo.needs_native(info["dist"])
                and router._norm(info["dist"]) not in have):
            break
    else:
        raise Skip("no pure-Python package on this Mac that the phone lacks")

    def uses():
        m = __import__(mod)
        return m.__name__, getattr(m, "__version__", None)
    return f"{mod}: " + _phone_vs_mac(ex, uses)


def _t_sneaky(n):  # looks pure, imports numpy at run time
    return float(__import__("numpy").arange(float(n)).sum())


def indirect_numpy(ex, quick):
    _need(ex, "numpy")
    import numpy as np
    got = ex.on("phone").submit(_t_sneaky, 1000).result(timeout=T)
    _expect(got == _t_sneaky(1000), f"returned {got!r}")
    x = np.arange(1000.0)
    after = ex.on("phone").submit(lambda a: float((a * 2).sum()), x).result(timeout=T)
    _expect(after == float((x * 2).sum()), f"numpy task after it returned {after!r}")
    return "right answer, and numpy works in the next task"


# ------------------------------------------------------------------ guards

def mac_file_guard(ex, quick):
    p = Path("/tmp") / f"pn-check-{uuid.uuid4().hex[:8]}.txt"
    p.write_text(text := f"written on the Mac as {p.name}")
    try:
        f = ex.submit(lambda: open(p).read())
        got = f.result(timeout=T)
        _expect(got == text, f"read {got!r}")
        _expect(f.placement.kind == "mac", f"auto placement ran it on {f.placement.kind}")
        _raises_placement(ex.on("phone").submit(lambda: open(p).read()), "reading a Mac file")
    finally:
        p.unlink(missing_ok=True)
    return "auto: Mac's content from the Mac; forced: PlacementError"


def _t_spawn():
    import subprocess
    return subprocess.run([sys.executable, "-c", "print(5)"], capture_output=True, text=True).stdout


def process_guard(ex, quick):
    _raises_placement(ex.on("phone").submit(_t_spawn), "starting a process")
    return "forced: PlacementError"


def _t_c_write():
    import ctypes
    import os
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.open(f"pn-check-{os.getpid()}.bin".encode(), os.O_CREAT | os.O_WRONLY, 0o644)
    if fd >= 0:
        os.close(fd)
    return fd, ctypes.get_errno()


def c_level_write(ex, quick):
    fd, err = ex.on("phone").submit(_t_c_write).result(timeout=T)
    _expect(fd < 0, "a relative path opened from C was writable on the phone")
    return f"open() refused (errno {err})"


# -------------------------------------------------------------------- data

def large_array_once(ex, quick):
    _need(ex, "numpy")
    import numpy as np
    mb = 16 if quick else 64
    x = np.random.default_rng().random((mb << 20) // 8)  # fresh content: never cached before
    ph, node = ex.on("phone"), _phone(ex)
    t0, b0 = time.perf_counter(), node.bytes_sent
    s = ph.submit(lambda a: float(a.sum()), x).result(timeout=T)
    d1, b1, t0 = time.perf_counter() - t0, node.bytes_sent, time.perf_counter()
    m = ph.submit(lambda a: float(a.max()), x).result(timeout=T)
    d2, b2 = time.perf_counter() - t0, node.bytes_sent
    _expect(math.isclose(s, float(x.sum()), rel_tol=1e-12) and m == float(x.max()), "wrong result")
    _expect(b2 - b1 < x.nbytes / 100, f"second use sent {(b2 - b1) / 2**20:.1f} MB again")
    # Bytes are the real test. Time is only telling when the first use was link-bound
    # (over USB it is; over loopback to a stand-in it's scheduling noise).
    _expect(d1 < 1 or d2 < d1 / 2, f"second use took {d2:.2f} s vs {d1:.2f} s first")
    return f"{mb} MB: first {d1:.2f} s ({(b1 - b0) / 2**20:.0f} MB sent), second {d2:.2f} s ({(b2 - b1) / 1024:.0f} KB)"


def pin_unpin(ex, quick):
    _need(ex, "numpy")
    import numpy as np
    x = np.random.default_rng().random(1 << 20)
    _expect(ex.pin(x) is x, "pin didn't return its argument")
    node = _phone(ex)
    b0 = node.bytes_sent
    got = ex.on("phone").submit(lambda a: float(a.sum()), x).result(timeout=T)
    sent = node.bytes_sent - b0
    _expect(math.isclose(got, float(x.sum()), rel_tol=1e-12), "wrong result while pinned")
    _expect(sent < x.nbytes / 10, f"task on pinned data sent {sent / 2**20:.1f} MB")
    ex.unpin(x)
    _expect(ex.on("phone").submit(lambda a: float(a.max()), x).result(timeout=T) == float(x.max()),
            "wrong result after unpin")
    return f"task on pinned data sent {sent / 1024:.0f} KB of {x.nbytes / 2**20:.0f} MB"


class _Tally:
    """Kept on the phone between calls."""

    def __init__(self, start):
        self.n = start

    def add(self, k):
        self.n += k
        return self.n


def object_on_phone(ex, quick):
    obj = ex.on("phone").create(_Tally, 10)
    try:
        _expect(obj.kind == "phone", f"kept on the {obj.kind}")
        got = [obj.add(i).result(timeout=T) for i in range(1, 6)]
        _expect(got == [11, 13, 16, 20, 25], f"calls returned {got}")
    finally:
        obj.close()
    return f"5 calls on one object kept on {obj.node}; its state carried over"


def _t_ticks(n, pause):
    for i in range(n):
        yield i
        time.sleep(pause)


def stream_from_phone(ex, quick):
    t0, first, got = time.perf_counter(), None, []
    for x in ex.on("phone").stream(_t_ticks, 5, 0.2):
        first = time.perf_counter() - t0 if first is None else first
        got.append(x)
    total = time.perf_counter() - t0
    _expect(got == list(range(5)), f"got {got}")
    _expect(first < total / 2, f"the first item came after {first:.2f} s of {total:.2f} s")
    return f"first item after {first:.2f} s, all 5 after {total:.2f} s"


# ---------------------------------------------------------------- executor

def map_equals_builtin(ex, quick):
    def f(a, b):
        return a * b + 1
    want = list(map(f, range(100), range(100, 200)))
    _expect(list(ex.map(f, range(100), range(100, 200), timeout=T)) == want, "map differs")
    _expect(list(ex.map(f, range(100), range(100, 200), timeout=T, chunksize=7)) == want, "chunked map differs")
    return "chunksize 1 and 7"


def placement_overrides(ex, quick):
    on_phone, on_mac = pn.runs_on("phone")(lambda: 3), pn.runs_on("mac")(lambda: 4)
    cases = [("ex.on('phone')", ex.on("phone").submit(lambda: 1), "phone"),
             ("ex.on('mac')", ex.on("mac").submit(lambda: 1), "mac"),
             ("runs_on('phone')", ex.submit(on_phone), "phone"), ("runs_on('mac')", ex.submit(on_mac), "mac")]
    for where in ("phone", "mac"):
        with pn.placement(where):
            cases.append((f"pn.placement({where!r})", ex.submit(lambda: 2), where))
    for label, f, want in cases:
        f.result(timeout=T)
        _expect(f.placement.kind == want, f"{label} ran on {f.placement.kind!r}")
    return f"{len(cases)} overrides landed where asked"


def _t_norm(i, n):
    import numpy as np
    return float(np.linalg.norm(np.arange(float(n)) * i))


def auto_placement(ex, quick):
    _need(ex, "numpy", phone=False)
    n = 50_000 if quick else 200_000
    futs = [ex.submit(_t_norm, i, n) for i in range(40)]
    vals = [f.result(timeout=T) for f in futs]
    _expect(all(math.isclose(v, _t_norm(i, n), rel_tol=1e-9) for i, v in enumerate(vals)), "wrong result")
    kinds = [f.placement.kind for f in futs]
    return f"40 correct: phone {kinds.count('phone')}, Mac {kinds.count('mac')}"


def _t_boom():
    raise ValueError("raised on purpose")


def error_surfaces(ex, quick):
    for where in ("phone", "auto"):
        try:
            ex.on(where).submit(_t_boom).result(timeout=T)
        except ValueError as e:
            _expect("_t_boom" in str(e.__cause__ or ""), f"{where}: no remote traceback")
        else:
            raise AssertionError(f"{where}: no exception")
    return "ValueError with remote traceback (forced and auto)"


# ------------------------------------------------------------ integrations

def joblib_gridsearch(ex, quick):
    _need(ex, "sklearn", "joblib", phone=False)
    from sklearn.datasets import make_classification
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import GridSearchCV
    X, y = make_classification(400, 10, random_state=0)
    grid = {"C": [0.01, 0.1, 1.0, 10.0]}
    want = GridSearchCV(LogisticRegression(max_iter=1000), grid, cv=3).fit(X, y)
    n0 = len(ex.history)
    with pn.parallel(ex):
        got = GridSearchCV(LogisticRegression(max_iter=1000), grid, cv=3, n_jobs=-1).fit(X, y)
    _expect(got.best_params_ == want.best_params_, f"best_params_ {got.best_params_} vs {want.best_params_}")
    _expect(math.isclose(got.best_score_, want.best_score_, rel_tol=1e-9), "best_score_ differs")
    return f"same best_params_ and best_score_; {_ran(ex, n0)}"


def pandas_accessor(ex, quick):
    _need(ex, "pandas", "numpy", phone=False)
    import numpy as np
    import pandas as pd
    from .integrations import pandas as _  # noqa: F401  (registers .pn)
    rng = np.random.default_rng(3)
    df = pd.DataFrame({"g": rng.integers(0, 20, 3000), "x": rng.normal(size=3000)})
    f, g, n0 = (lambda d: d.x.abs().sum()), (lambda v: v * 3), len(ex.history)
    import warnings
    with warnings.catch_warnings():  # pandas warns that groupby().apply will stop passing the grouping column
        warnings.simplefilter("ignore")
        _expect(df.pn.groupby("g", executor=ex).apply(f).equals(df.groupby("g").apply(f)), "groupby().apply differs")
        _expect(df.x.pn.map(g, executor=ex).equals(df.x.map(g)), "Series.pn.map differs")
    return f"groupby().apply and Series.map equal pandas; {_ran(ex, n0)}"


def dask_scheduler(ex, quick):
    _need(ex, "dask", "numpy", phone=False)
    import dask
    import dask.array as da
    x = da.arange(200_000, chunks=20_000, dtype=float)
    total = dask.delayed(sum)([dask.delayed(pow)(i, 2) for i in range(20)])
    n0 = len(ex.history)
    got = dask.compute((x ** 2).sum(), total, scheduler=ex)
    want = dask.compute((x ** 2).sum(), total, scheduler="threads")
    _expect(_close(list(got), list(want), 1e-12), f"{_short(got)} vs {_short(want)}")
    return f"equals the threaded scheduler; {_ran(ex, n0)}"


def ipython_magic(ex, quick):
    _need(ex, "IPython", phone=False)
    import builtins
    import types
    from IPython.core.interactiveshell import InteractiveShell
    from traitlets.config import Config
    c = Config()
    c.HistoryManager.enabled = False
    had, prev = set(vars(builtins)), pn._current
    pn._current = ex  # the magics use the current executor
    try:
        sh = InteractiveShell.instance(config=c, user_module=types.ModuleType("pn_check_ns"))
        sh.run_line_magic("load_ext", "pocketnode")
        sh.user_ns["pn_xs"] = [1.5, 2.5, 3.0]
        last = sh.run_cell_magic("phone", "", "pn_total = sum(pn_xs) * 2\npn_total + 1\n")
        got = sh.user_ns.get("pn_total")
    finally:  # leave the process as it was
        pn._current = prev
        InteractiveShell.clear_instance()
        for k in set(vars(builtins)) - had:
            delattr(builtins, k)
    _expect(got == 14.0 and last == 15.0, f"pn_total in the notebook is {got!r}, last expression {last!r}")
    return "read a notebook variable, assigned a new one, returned the last expression"


# ----------------------------------------------------------------- reports

def stacks_text(ex, quick):
    text = ex.stacks("phone", timeout=30)
    _expect(isinstance(text, str) and text.strip(), "empty")
    return f"{len(text.splitlines())} lines"


def describe_explain(ex, quick):
    d, e = ex.describe(), ex.explain()
    _expect(isinstance(d, str) and "phone" in d, f"describe() gave {d!r}")
    _expect(isinstance(e, str) and e.strip(), "explain() is empty")
    return f"describe {len(d.splitlines())} lines, explain {len(e.splitlines())} lines"


def _t_work(tag, i):
    time.sleep(0.05)
    return tag, i


def _store_phone(ex):
    node = _phone(ex)
    if node.hello.get("edition") != "store":
        raise Skip("only for a phone running the App Store edition")
    return node


def store_install_refused(ex, quick):
    from .provision import hot
    from .storeworker import FIXED
    _store_phone(ex)
    try:
        hot.add(ex, ["numpy"])
    except hot.CantAdd as e:
        _expect(str(e).startswith(FIXED), f"refused, but with {str(e)[:120]!r}")
        return "adding a package is refused with the fixed-set sentence"
    raise AssertionError("hot.add didn't refuse")


def _t_visible(x):
    return x + 1


def store_code_visible(ex, quick):
    import hashlib
    import inspect
    import textwrap
    node = _store_phone(ex)
    _expect(ex.on("phone").submit(_t_visible, 1).result(timeout=T) == 2, "wrong result")
    req = uuid.uuid4().hex
    reply = ex.expect(req)
    node.post({"t": "code_log", "req": req})
    msg = reply(30)
    _expect(msg is not None, "the phone didn't answer the code-log request")
    rows = [r for r in msg.get("codes", []) if "_t_visible" in str(r.get("label"))]
    _expect(rows, "the phone's code log has no entry for _t_visible")
    src = textwrap.dedent(inspect.getsource(_t_visible))
    _expect(rows[0].get("srckind") == "source", f"logged as {rows[0].get('srckind')!r}, not source")
    _expect(rows[0].get("src_digest") == hashlib.sha256(src.encode()).hexdigest()[:16],
            "the source the phone holds isn't the function's")
    return f"logged {rows[0]['label']} with its source"


def two_sessions(ex, quick):
    from .executor import PocketExecutor
    from .sources import Address, PhoneLink
    src = _phone(ex).source
    if isinstance(src, Address):
        second = Address(src.addr, token=src.token)
    elif isinstance(src, PhoneLink):
        second = PhoneLink(udid=src.udid, port=src.port, token=src.token, relaunch=False)
    else:
        raise Skip("the stand-in spawned by simulate_phone takes one session")
    other = PocketExecutor(notes=False)
    try:
        other.add_source(second)
        _expect(other.wait_for("phone", 30), "the second session didn't connect")
        fa = [ex.on("phone").submit(_t_work, "a", i) for i in range(20)]
        fb = [other.on("phone").submit(_t_work, "b", i) for i in range(20)]
        _expect([f.result(timeout=T) for f in fa] == [("a", i) for i in range(20)], "first session wrong")
        _expect([f.result(timeout=T) for f in fb] == [("b", i) for i in range(20)], "second session wrong")
    finally:
        other.shutdown(wait=False)
    return "both sessions got their own results"


# --------------------------------------------------------------------- run

# Pure-Python tasks run before torch: subinterpreters once broke torch afterwards.
CHECKS = [
    ("numpy linear algebra", _library("numpy", _t_linalg, (200,), (400,))),
    ("pandas groupby", _library("pandas", _t_groupby, (2_000,), (20_000,))),
    ("scipy optimize", _library("scipy", _t_optimize)),
    ("scikit-learn fit n_jobs=-1", _library("sklearn", _t_forest, (500, 20), (2000, 50))),
    ("pyarrow parquet in memory", _library("pyarrow", _t_parquet, (1_000,), (20_000,))),
    ("polars lazy query", _library("polars", _t_polars, (1_000,), (50_000,))),
    ("pure-Python task", pure_task),
    ("pure-Python parallelism", pure_parallel),
    ("pure package from the Mac", pure_package_on_demand),
    ("numpy via __import__", indirect_numpy),
    ("torch training (after pure)", _library("torch", _t_torch, tol=1e-4)),
    ("torch on the phone's GPU", torch_gpu),
    ("PyTorch model crosses once, kept on the GPU", torch_model_once),
    ("MLX on the phone's GPU", mlx_gpu),
    ("Mac file guard", mac_file_guard),
    ("process guard", process_guard),
    ("C-level relative write", c_level_write),
    ("large array crosses once", large_array_once),
    ("pin / unpin", pin_unpin),
    ("object kept on the phone", object_on_phone),
    ("streaming from the phone", stream_from_phone),
    ("map equals builtin map", map_equals_builtin),
    ("placement overrides", placement_overrides),
    ("auto placement, 40 tasks", auto_placement),
    ("task error surfaces", error_surfaces),
    ("joblib GridSearchCV", joblib_gridsearch),
    ("pandas .pn accessor", pandas_accessor),
    ("dask scheduler=ex", dask_scheduler),
    ("IPython %%phone", ipython_magic),
    ("ex.stacks('phone')", stacks_text),
    ("describe() / explain()", describe_explain),
    ("two sessions share the phone", two_sessions),
    ("store: install is refused", store_install_refused),
    ("store: code is visible", store_code_visible),
]
SLOW = {"torch training (after pure)", "joblib GridSearchCV"}  # skipped with quick=True
LABEL = {True: "PASS", False: "FAIL", None: "SKIP"}


def _one(fn, ex, quick) -> tuple[bool | None, str]:
    box: dict = {}

    def go():
        try:
            box["r"] = (True, fn(ex, quick) or "")
        except Skip as e:
            box["r"] = (None, str(e))
        except BaseException as e:  # noqa: BLE001 - every item is isolated
            msg = str(e).strip().splitlines()
            box["r"] = (False, f"{type(e).__name__}: {msg[0]}"[:200] if msg else type(e).__name__)

    t = threading.Thread(target=go, daemon=True, name=f"pn-check-{fn.__name__}")
    t.start()
    t.join(CHECK_TIMEOUT)
    return box.get("r", (False, f"timed out after {CHECK_TIMEOUT} s"))


def run(ex, quick: bool = False) -> list[tuple[str, bool | None, str]]:
    """Run every item, printing each as it finishes; (name, passed, detail), passed None for a skip."""
    width = max(len(n) for n, _ in CHECKS)
    results = []
    for name, fn in CHECKS:
        ok, detail = (None, "skipped with --quick") if quick and name in SLOW else _one(fn, ex, quick)
        results.append((name, ok, detail))
        print(f"{LABEL[ok]}  {name:<{width}}  {detail}", flush=True)
    p, f = sum(r[1] is True for r in results), sum(r[1] is False for r in results)
    print(f"{p} passed, {f} failed, {len(results) - p - f} skipped", flush=True)
    return results


def main(ex, quick: bool = False) -> int:
    """Say what's connected and run every item. Returns the exit status: 1 if anything failed."""
    h = _phone(ex).hello
    print(f"{h['name']}: {h.get('model', '?')}, Python {'.'.join(map(str, h['python']))}, "
          f"{h['slots']} slots, {len(h.get('packages', {}))} packages"
          + (", App Store edition (fixed package set)" if h.get("edition") == "store" else ""), flush=True)
    return 1 if any(ok is False for _, ok, _ in run(ex, quick)) else 0
