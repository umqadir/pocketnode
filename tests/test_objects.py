"""Objects kept on a node: ex.create(...) builds one there, and its methods run against it."""

from __future__ import annotations

import time
import uuid

import numpy as np
import pytest

from test_core import SIM_PY

import pocketnode as pn
from pocketnode.errors import NodeLost, PlacementError


class Counter:
    def __init__(self, start=0, size=1000):
        self.n = start
        self.acc = np.zeros(size)

    def bump(self, by=1):
        self.n += by
        return self.n

    def add(self, x):
        self.acc += x
        return float(self.acc.sum())

    def read(self, x, i):
        return float(x[i]) + self.n

    def nap(self, seconds):
        import time
        time.sleep(seconds)
        return self.n

    def where(self):
        import sys
        return sys.prefix


class Spans:
    """Records when each call ran, to show calls never overlap."""

    def __init__(self):
        self.spans = []

    def work(self, i, seconds=0.2):
        import time
        t0 = time.time()
        time.sleep(seconds)
        self.spans.append((i, t0, time.time()))
        return i

    def get(self):
        return self.spans


class Broken:
    def __init__(self):
        raise ValueError("no constructing this")


class Faulty:
    def fail(self):
        raise KeyError("from a method")

    def touch_mac(self, path):
        return open(path).read()


@pytest.fixture(scope="module")
def ex():
    if not SIM_PY.exists():
        pytest.skip("no phone stand-in env (.phone-sim)")
    e = pn.connect(mac_workers=2, phone=False, simulate_phone=str(SIM_PY), notes=False, auto_add=False)
    assert e.wait_for("phone", 30) and e.wait_for("mac", 30)
    yield e
    e.shutdown(wait=False)


def _phone(ex):
    return next(n for n in list(ex.nodes.values()) if n.kind == "phone")


def _inventory(ex, node):
    req = uuid.uuid4().hex
    reply = ex.expect(req)
    node.post({"t": "inventory", "req": req})
    return reply(10)


def test_state_is_kept_between_calls(ex):
    c = ex.on("phone").create(Counter, 10, size=50_000)
    assert c.kind == "phone" and c.node == _phone(ex).name
    assert [c.bump().result(timeout=30) for _ in range(3)] == [11, 12, 13]
    assert c.bump(by=5).result(timeout=30) == 18
    assert c.add(np.ones(50_000)).result(timeout=30) == 50_000.0
    assert c.add(np.ones(50_000)).result(timeout=30) == 100_000.0, "the array lives on in the object"
    c.close()


def test_automatic_and_forced_placement(ex):
    auto = ex.create(Counter)
    assert auto.kind == "phone", "an object that can run on the phone goes there"
    assert auto.where().result(timeout=30).endswith(".phone-sim")
    mac = ex.on("mac").create(Counter)
    assert mac.kind == "mac" and not mac.where().result(timeout=30).endswith(".phone-sim")
    with ex.on("phone").create(Counter) as ph:
        assert ph.kind == "phone"
    auto.close()
    mac.close()


def test_calls_on_one_object_run_in_order_and_never_overlap(ex):
    s = ex.on("phone").create(Spans)
    futs = [s.work(i) for i in range(5)]
    assert [f.result(timeout=30) for f in futs] == list(range(5))
    spans = s.get().result(timeout=30)
    assert [i for i, _, _ in spans] == list(range(5))
    assert all(spans[k][2] <= spans[k + 1][1] + 1e-3 for k in range(4)), spans
    s.close()


def test_two_objects_run_at_once(ex):
    a, b = ex.on("phone").create(Spans), ex.on("phone").create(Spans)
    t0 = time.time()
    futs = [o.work(i, 0.5) for i in range(3) for o in (a, b)]
    [f.result(timeout=30) for f in futs]
    assert time.time() - t0 < 2.6, "two objects' calls overlapped (3 x 0.5 s each, run side by side)"
    a.close()
    b.close()


def test_constructor_and_method_errors_come_back(ex):
    with pytest.raises(ValueError, match="no constructing this") as ei:
        ex.on("phone").create(Broken)
    assert "__init__" in str(ei.value.__cause__)
    f = ex.on("phone").create(Faulty)
    with pytest.raises(KeyError, match="from a method") as ei:
        f.fail().result(timeout=30)
    assert "def fail" in str(ei.value.__cause__) or "in fail" in str(ei.value.__cause__)
    with pytest.raises(AttributeError):
        f.no_such_method().result(timeout=30)
    f.close()


def test_guards_apply_inside_methods(ex, tmp_path):
    p = f"/tmp/pn-objects-{uuid.uuid4().hex[:8]}.txt"
    open(p, "w").write("mac")
    try:
        f = ex.on("phone").create(Faulty)
        with pytest.raises(PlacementError, match="on the Mac"):
            f.touch_mac(p).result(timeout=30)
        f.close()
    finally:
        import os
        os.unlink(p)


def test_a_call_refused_before_it_runs_lets_the_next_one_go(ex):
    pd = pytest.importorskip("pandas")
    c = ex.on("phone").create(Counter)
    bad = c.read(pd.Series([1.0]), 0)  # pandas (compiled) isn't on the stand-in phone
    good = c.bump()
    with pytest.raises(PlacementError, match="pandas"):
        bad.result(timeout=30)
    assert good.result(timeout=30) == 1
    c.close()


def test_large_argument_crosses_once(ex):
    c = ex.on("phone").create(Counter)
    x = np.random.default_rng().random(2_000_000)  # 16 MB
    node = _phone(ex)
    b0 = node.bytes_sent
    got = [c.read(x, i).result(timeout=30) for i in range(10)]
    assert got == [float(x[i]) for i in range(10)]
    assert node.bytes_sent - b0 < x.nbytes * 1.5
    c.close()


def test_close_frees_it_on_the_node(ex):
    c = ex.on("phone").create(Counter)
    c.bump().result(timeout=30)
    node = _phone(ex)
    assert c._id in _inventory(ex, node)["objects"]
    c.close()
    end = time.time() + 10
    while c._id in _inventory(ex, node)["objects"] and time.time() < end:
        time.sleep(0.1)
    assert c._id not in _inventory(ex, node)["objects"]
    with pytest.raises(RuntimeError, match="closed"):
        c.bump()


def test_older_app_gives_a_clear_error(ex):
    node = _phone(ex)
    features = node.hello.pop("features")
    try:
        with pytest.raises(PlacementError, match=r"older than this pocketnode; run `pocketnode app install`"):
            ex.on("phone").create(Counter)
        assert ex.create(Counter).kind == "mac", "automatic placement uses a Mac worker instead"
    finally:
        node.hello["features"] = features


def _kill_phone(ex):
    old = _phone(ex).id
    src = next(s for s in ex.sources if s.describe() == "simulated phone")
    for p in src.procs:
        p.kill()
    return old


def _wait_new_phone(ex, old):
    end = time.time() + 30
    while time.time() < end:
        if any(n.kind == "phone" and n.id != old for n in list(ex.nodes.values())):
            return
        time.sleep(0.1)
    raise AssertionError("the stand-in phone didn't come back")


def test_lost_node_then_rebuilt(ex, monkeypatch):
    said = []
    monkeypatch.setattr(ex, "_note", lambda key, text: said.append(text))
    c = ex.on("phone").create(Counter, 100)
    keep = ex.on("phone").create(Counter, 100, recreate=False)
    assert c.bump().result(timeout=30) == 101 and keep.bump().result(timeout=30) == 101
    slow = c.nap(10)
    time.sleep(1.0)
    old = _kill_phone(ex)
    with pytest.raises(NodeLost, match="Counter") as ei:
        slow.result(timeout=30)
    assert ei.value.obj == "Counter" and ei.value.node
    _wait_new_phone(ex, old)
    assert c.bump().result(timeout=30) == 101, "rebuilt from its arguments: its state started over"
    assert c.bump().result(timeout=30) == 102
    assert sum("rebuilt" in t and "state" in t for t in said) == 1, said
    with pytest.raises(NodeLost, match="recreate=False"):
        keep.bump().result(timeout=30)
    c.close()
