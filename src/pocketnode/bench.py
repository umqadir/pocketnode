"""Measurements and on-device experiments (HANDOFF §9 step 2).

Everything here runs from the Mac through the normal executor, so the phone
proves itself with numbers rather than assumptions:

    python -m pocketnode.cli bench            # link + compute, phone vs Mac
    python -m pocketnode.cli experiments      # the design-deciding device tests

Results are saved under ~/.pocketnode/experiments/.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .sources import CONFIG_DIR

OUT = CONFIG_DIR / "experiments"


# ------------------------------------------------------------- phone side
# (plain functions: they're pickled by reference and run inside the worker)

def _noop():
    return None


def _cpu_info():
    import os
    import platform
    import sys
    from pocketnode import device
    return {"platform": sys.platform, "machine": device.model(), "cores": device.core_layout(),
            "python": sys.version.split()[0], "mem_total": device.total_memory(),
            "mem_available": device.available_memory(), "thermal": device.thermal_state(),
            "cpu_count": os.cpu_count(), "release": platform.release()}


def _matmul_gflops(n: int, dtype: str, threads: int | None, seconds: float = 2.0):
    import numpy as np
    from threadpoolctl import threadpool_limits
    a = np.random.default_rng(0).random((n, n)).astype(dtype)
    b = a.copy()
    with threadpool_limits(threads):
        a @ b
        reps, t0 = 0, time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            a @ b
            reps += 1
        dt = time.perf_counter() - t0
    return 2 * n**3 * reps / dt / 1e9


def _python_loop(n: int = 3_000_000):
    t0 = time.perf_counter()
    s = 0
    for i in range(n):
        s += i * i % 7
    return n / (time.perf_counter() - t0) / 1e6  # M iterations/s


def _sklearn_fit():
    from sklearn.datasets import make_classification
    from sklearn.ensemble import RandomForestClassifier
    X, y = make_classification(5000, 40, random_state=0)
    t0 = time.perf_counter()
    RandomForestClassifier(100, n_jobs=-1, random_state=0).fit(X, y)
    return time.perf_counter() - t0


def _torch_step():
    import torch
    m = torch.nn.Sequential(torch.nn.Linear(512, 1024), torch.nn.ReLU(), torch.nn.Linear(1024, 10))
    x, y = torch.randn(256, 512), torch.randint(0, 10, (256,))
    opt = torch.optim.SGD(m.parameters(), lr=0.01)
    t0 = time.perf_counter()
    for _ in range(50):
        opt.zero_grad()
        torch.nn.functional.cross_entropy(m(x), y).backward()
        opt.step()
    return (time.perf_counter() - t0) / 50


def _mlx_gpu():
    import time
    import mlx.core as mx
    info = {k: v for k, v in mx.metal.device_info().items()
            if k in ("architecture", "device_name", "memory_size", "max_recommended_working_set_size")}
    out = {"metal": mx.metal.is_available(), **info}
    for n in (1024, 4096):
        a = mx.random.normal((n, n), dtype=mx.float16)
        mx.eval(a @ a)
        t0, reps = time.perf_counter(), 0
        while time.perf_counter() - t0 < 2:
            mx.eval(a @ a)
            reps += 1
        out[f"fp16_matmul_{n}_tflops"] = round(2 * n**3 * reps / (time.perf_counter() - t0) / 1e12, 2)
    return out


def _heartbeat(seconds: float, period: float = 0.25):
    """Record wall-clock ticks; gaps show when the process was suspended."""
    ticks, t_end = [], time.time() + seconds
    while time.time() < t_end:
        ticks.append(time.time())
        time.sleep(period)
    return ticks


def _burn(seconds: float, n: int = 768):
    """Sustained BLAS load; samples throughput and thermal state over time."""
    import numpy as np
    from pocketnode import device
    a = np.random.default_rng(0).random((n, n)).astype("float32")
    samples, t_end = [], time.time() + seconds
    while time.time() < t_end:
        reps, t0 = 0, time.perf_counter()
        while time.perf_counter() - t0 < 5:
            a @ a
            reps += 1
        samples.append((time.time(), 2 * n**3 * reps / (time.perf_counter() - t0) / 1e9, device.thermal_state()))
    return samples


def _memory_probe(step_mb: int = 256, margin_mb: int = 300):
    import numpy as np
    from pocketnode import device
    start = device.available_memory()
    held, got = [], 0
    while True:
        avail = device.available_memory()
        if avail is not None and avail < (step_mb + margin_mb) * 2**20:
            break
        if avail is None and got > 8 * 2**30:
            break
        try:
            held.append(np.ones(step_mb * 2**20 // 8))
        except MemoryError:
            break
        got += step_mb * 2**20
    end = device.available_memory()
    del held
    return {"available_at_start": start, "allocated": got, "available_after": end}


def _dlopen_test(data: bytes, name: str, symbol: str):
    import ctypes
    import pathlib
    import tempfile
    d = pathlib.Path(tempfile.gettempdir()) / "pn-dlopen"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_bytes(data)
    try:
        lib = ctypes.CDLL(str(p))
        f = getattr(lib, symbol)
        f.restype = ctypes.c_int
        return {"ok": True, "value": f()}
    except OSError as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------- mac side

def _node(ex, kind):
    return next((n for n in ex.nodes.values() if n.kind == kind), None)


def link(ex, mb: int = 64, chunk_mb: int = 8) -> dict:
    """Round-trip latency and throughput to the phone in both directions."""
    node = _node(ex, "phone")
    lat = []
    for _ in range(30):
        t0 = time.perf_counter()
        ex.on("phone").submit(_noop).result()
        lat.append(time.perf_counter() - t0)
    buf = os.urandom(chunk_mb << 20)
    got = threading.Event()
    res = {}
    orig = getattr(node.source, "on_echo", None)

    def on_echo(n, msg, bufs):
        res.setdefault("msgs", []).append((time.perf_counter(), msg.get("n"), sum(len(b) for b in bufs)))
        if len(res["msgs"]) == res["want"]:
            got.set()

    node.source.on_echo = on_echo
    try:
        out = {}
        for direction, back in (("mac_to_phone", False), ("round_trip", True)):
            got.clear()
            res.clear()
            res["want"] = mb // chunk_mb
            t0 = time.perf_counter()
            for _ in range(res["want"]):
                node.conn.send({"t": "echo", "back": back}, [buf])
            got.wait(120)
            dt = time.perf_counter() - t0
            moved = mb * (2 if back else 1)
            out[direction + "_MBps"] = round(moved / dt, 1)
    finally:
        if orig is None:
            del node.source.on_echo
        else:
            node.source.on_echo = orig
    lat.sort()
    out.update(rtt_ms_median=round(lat[len(lat) // 2] * 1e3, 2), rtt_ms_p90=round(lat[int(len(lat) * 0.9)] * 1e3, 2))
    return out


def compute(ex) -> dict:
    """The same work on one Mac worker and on the phone."""
    out = {}
    jobs = {
        "python_Mloops_per_s": (_python_loop, ()),
        "matmul_f32_1t_gflops": (_matmul_gflops, (1024, "float32", 1)),
        "matmul_f64_1t_gflops": (_matmul_gflops, (1024, "float64", 1)),
        "matmul_f32_all_gflops": (_matmul_gflops, (2048, "float32", None)),
        "sklearn_rf_fit_s": (_sklearn_fit, ()),
        "torch_mlp_step_s": (_torch_step, ()),
        "mlx_gpu": (_mlx_gpu, ()),
    }
    for name, (fn, args) in jobs.items():
        row = {}
        for where in ("phone", "mac"):
            try:
                row[where] = round(ex.on(where).submit(fn, *args).result(timeout=600), 4)
            except Exception as e:
                row[where] = f"error: {type(e).__name__}: {str(e)[:120]}"
        out[name] = row
    return out


def background(ex, udid: str, seconds: float = 120, other_app: str = "com.apple.Preferences") -> dict:
    """How long the phone keeps computing after the user switches apps."""
    from .provision.app import config
    from .sources import BUNDLE_ID
    fut = ex.on("phone").submit(_heartbeat, seconds + 30)
    time.sleep(10)
    t_bg = time.time()
    subprocess.run(["xcrun", "devicectl", "device", "process", "launch", "--device", udid, other_app],
                   capture_output=True)
    time.sleep(seconds)
    subprocess.run(["xcrun", "devicectl", "device", "process", "launch", "--device", udid,
                    config().get("bundle_id", BUNDLE_ID)], capture_output=True)
    ticks = fut.result(timeout=seconds + 600)
    gaps = [(a, b - a) for a, b in zip(ticks, ticks[1:]) if b - a > 2]
    ran_after_bg = max((t for t in ticks if t >= t_bg), default=t_bg) - t_bg
    first_gap = next(((a - t_bg) for a, g in gaps if a >= t_bg - 1), None)
    return {"backgrounded_for_s": seconds, "kept_running_s": round(first_gap if first_gap is not None else ran_after_bg, 1),
            "gaps": [(round(a - t_bg, 1), round(g, 1)) for a, g in gaps]}


def thermal(ex, seconds: float = 600) -> dict:
    samples = ex.on("phone").submit(_burn, seconds).result(timeout=seconds + 120)
    first = samples[0][1]
    return {"gflops_start": round(first, 1), "gflops_end": round(samples[-1][1], 1),
            "stability_pct": round(100 * min(s[1] for s in samples) / max(s[1] for s in samples), 1),
            "states": sorted({s[2] for s in samples}),
            "series": [(round(t - samples[0][0]), round(g, 1), st) for t, g, st in samples]}


def memory(ex) -> dict:
    return ex.on("phone").submit(_memory_probe).result(timeout=300)


def dlopen_after_install(ex, identity: str | None = None, sdk: str = "iphoneos") -> dict:
    """Can the app load native code signed after it was installed? (HANDOFF §3's open question)"""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "t.c"
        src.write_text("int pocketnode_probe(void) { return 42; }\n")
        lib = Path(d) / "probe.dylib"
        target = "arm64-apple-ios17.0" + ("-simulator" if sdk == "iphonesimulator" else "")
        subprocess.run(["xcrun", "--sdk", sdk, "clang", "-target", target, "-dynamiclib", str(src), "-o", str(lib)],
                       check=True, capture_output=True)
        results = {}
        identity = identity or signing_identity()
        variants = {"adhoc": "-"}
        if identity:
            variants["team_identity"] = identity
        for label, ident in variants.items():
            v = Path(d) / f"probe_{label}.dylib"
            v.write_bytes(lib.read_bytes())
            subprocess.run(["codesign", "-f", "-s", ident, "--timestamp=none", str(v)], capture_output=True)
            results[label] = ex.on("phone").submit(_dlopen_test, v.read_bytes(), v.name, "pocketnode_probe").result()
        return results


def signing_identity() -> str | None:
    out = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True,
                         text=True).stdout
    for line in out.splitlines():
        if "Apple Development" in line:
            return line.split('"')[1]
    return None


def save(name: str, data: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.json"
    p.write_text(json.dumps(data, indent=1, default=str))
    return p
