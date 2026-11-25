"""Streaming: items a generator yields on a node arrive on the Mac as they are produced."""

from __future__ import annotations

import time

import numpy as np
import pytest

from test_core import SIM_PY

import pocketnode as pn
from pocketnode.errors import NodeLost, PlacementError
from pocketnode.executor import STREAM_WINDOW


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


def slow_count(n, pause):
    import time
    for i in range(n):
        yield i
        time.sleep(pause)


def test_items_arrive_while_the_generator_runs(ex):
    t0 = time.time()
    seen = []
    for i in ex.on("phone").stream(slow_count, 4, 0.5):
        seen.append((i, time.time() - t0))
    assert [i for i, _ in seen] == [0, 1, 2, 3]
    assert seen[0][1] < 1.0 and seen[-1][1] > 1.4, f"arrival times {seen}"


def test_order_and_arrays(ex):
    def arrays(n):
        import numpy as np
        buf = np.zeros(100_000)
        for i in range(n):
            buf[:] = i  # the same array, changed in place: each item is what it was when yielded
            yield buf

    got = list(ex.on("phone").stream(arrays, 6))
    assert [float(a[0]) for a in got] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert all(a.shape == (100_000,) for a in got)


def test_exception_mid_stream(ex):
    def two_then_fail():
        yield 1
        yield 2
        raise ValueError("broke after two")

    got = []
    with pytest.raises(ValueError, match="broke after two") as ei:
        for x in ex.on("phone").stream(two_then_fail):
            got.append(x)
    assert got == [1, 2]
    assert "two_then_fail" in str(ei.value.__cause__)


def forever():
    import time
    i = 0
    while True:
        yield i
        i += 1
        time.sleep(0.01)


def test_leaving_early_stops_the_generator_and_frees_its_slot(ex):
    node = _phone(ex)
    for i in ex.on("phone").stream(forever):
        if i == 3:
            break
    assert not any(a.task.stream is not None for a in list(node.running.values())), "slot freed on the Mac"
    end = time.time() + 10
    while time.time() < end:
        ex.on("phone").submit(lambda: 0).result(timeout=30)
        if node.state.get("running") == 0:
            break
        time.sleep(0.1)
    assert node.state.get("running") == 0, "the generator stopped on the phone"
    s = ex.on("phone").stream(forever)
    assert next(s) == 0
    s.close()
    with pytest.raises(StopIteration):
        next(s)


def test_backpressure(ex):
    def many(n):
        yield from range(n)

    s = ex.on("phone").stream(many, 10_000)
    first = next(s)
    time.sleep(1.0)  # a slow reader: the phone must wait rather than pile items up here
    rest = list(s)
    assert [first, *rest] == list(range(10_000))
    assert s.max_buffered <= STREAM_WINDOW, s.max_buffered


def test_not_a_generator(ex):
    with pytest.raises(TypeError, match="not an iterator"):
        list(ex.on("phone").stream(lambda: [1, 2, 3]))


def test_automatic_placement(ex):
    def where():
        import sys
        import numpy as np
        yield sys.prefix
        yield float(np.ones(3).sum())

    s = ex.stream(where)
    first, total = list(s)
    assert first.endswith(".phone-sim") and total == 3.0
    assert s.placement.kind == "phone"


def test_method_stream_keeps_state(ex):
    class Gen:
        def __init__(self):
            self.made = 0

        def tokens(self, n):
            for i in range(n):
                self.made += 1
                yield f"t{self.made}"

        def count(self):
            return self.made

    g = ex.on("phone").create(Gen)
    assert list(g.tokens.stream(3)) == ["t1", "t2", "t3"]
    assert list(g.tokens.stream(2)) == ["t4", "t5"]
    for _ in g.tokens.stream(100):
        break  # stopped early: the next call still runs, after it
    assert g.count().result(timeout=30) >= 6
    g.close()


def test_older_app(ex):
    node = _phone(ex)
    features = node.hello.pop("features")
    try:
        with pytest.raises(PlacementError, match="older than this pocketnode"):
            list(ex.on("phone").stream(slow_count, 2, 0))
    finally:
        node.hello["features"] = features


def test_node_lost_mid_stream(ex):
    old = _phone(ex).id
    s = ex.on("phone").stream(slow_count, 100, 0.1)
    assert next(s) == 0
    src = next(x for x in ex.sources if x.describe() == "simulated phone")
    for p in src.procs:
        p.kill()
    with pytest.raises(NodeLost):
        for _ in s:
            pass
    end = time.time() + 30
    while time.time() < end and not any(n.kind == "phone" and n.id != old for n in list(ex.nodes.values())):
        time.sleep(0.1)
