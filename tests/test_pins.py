"""ex.pin(obj): the object itself is kept on each node, so tasks that use it don't rebuild it."""

from __future__ import annotations

import time
import uuid

import numpy as np
import pytest

from test_core import SIM_PY

import pocketnode as pn
from pocketnode import executor as ex_mod
from pocketnode.errors import PlacementError


class Model:
    """Counts, per node, how many times it was rebuilt there (unpickled)."""

    def __init__(self, w, tag):
        self.w, self.tag, self.marker = w, tag, 0

    def __call__(self, batch):
        return float((batch * self.w).sum())

    def __setstate__(self, state):
        import sys
        builds = sys.__dict__.setdefault("_pn_test_builds", {})
        builds[state["tag"]] = builds.get(state["tag"], 0) + 1
        self.__dict__.update(state)


def _builds(tag):
    import sys
    return sys.__dict__.get("_pn_test_builds", {}).get(tag, 0)


@pytest.fixture(scope="module")
def ex():
    if not SIM_PY.exists():
        pytest.skip("no phone stand-in env (.phone-sim)")
    e = pn.connect(mac_workers=2, phone=False, simulate_phone=str(SIM_PY), notes=False, auto_add=False)
    assert e.wait_for("phone", 30) and e.wait_for("mac", 30)
    yield e
    e.shutdown(wait=False)


def _phone(ex):
    return next(n for n in ex.nodes.values() if n.kind == "phone")


def _inventory(ex, node):
    req = uuid.uuid4().hex
    reply = ex.expect(req)
    node.post({"t": "inventory", "req": req})
    return reply(10)


def test_pinned_object_is_built_once_per_node(ex):
    tag = uuid.uuid4().hex
    model = Model(np.random.default_rng(0).random(200_000), tag)  # 1.6 MB of weights
    batches = [np.full(200_000, float(i)) for i in range(40)]
    ex.pin(model)

    def predict(batch):
        import os
        return model(batch), _builds(tag), os.getpid()

    try:
        got = list(ex.map(predict, batches)) + [f.result(timeout=30) for f in
                                                [ex.on("phone").submit(predict, b) for b in batches[:10]]
                                                + [ex.on("mac").submit(predict, b) for b in batches[:10]]]
        want = [model(b) for b in batches] + [model(b) for b in batches[:10]] * 2
        assert [g[0] for g in got] == want
        per_node = {}
        for _, builds, pid in got:
            per_node.setdefault(pid, set()).add(builds)
        assert len(per_node) >= 2, "ran on the phone and on Mac workers"
        assert all(b == {1} for b in per_node.values()), f"builds per node: {per_node}"
        # Once kept there, a task that uses it sends none of its weights.
        node = _phone(ex)
        b0 = node.bytes_sent
        ex.on("phone").submit(predict, batches[0]).result(timeout=30)
        assert node.bytes_sent - b0 < model.w.nbytes / 2 + batches[0].nbytes
    finally:
        ex.unpin(model)
    assert pn_kept_gone(ex, _phone(ex))


def pn_kept_gone(ex, node, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if not _inventory(ex, node)["kept"]:
            return True
        time.sleep(0.1)
    return False


def test_object_inside_a_container_argument(ex):
    tag = uuid.uuid4().hex
    model = Model(np.arange(100_000.0), tag)
    ex.pin(model)
    try:
        def use(items):
            return items["m"](np.ones(100_000)), _builds(tag)

        r = [ex.on("phone").submit(use, {"m": model}).result(timeout=30) for _ in range(5)]
        assert [x[0] for x in r] == [float(model.w.sum())] * 5 and {x[1] for x in r} == {1}
    finally:
        ex.unpin(model)


def test_kept_copy_is_a_snapshot_until_pinned_again(ex):
    tag = uuid.uuid4().hex
    model = Model(np.zeros(100_000), tag)
    model.marker = 1
    ex.pin(model)
    try:
        read = lambda: model.marker  # noqa: E731
        assert ex.on("phone").submit(read).result(timeout=30) == 1
        model.marker = 2
        assert ex.on("phone").submit(read).result(timeout=30) == 1, "a kept copy doesn't follow the Mac"
        ex.pin(model)  # pinned again: the change goes over
        assert ex.on("phone").submit(read).result(timeout=30) == 2
        assert ex.on("phone").submit(lambda: (model.tag, _builds(tag))).result(timeout=30)[1] == 2
    finally:
        ex.unpin(model)


def test_detected_changes_repin_by_themselves(ex, monkeypatch):
    # PyTorch tensors carry version counters; this stands in for them.
    monkeypatch.setattr(ex_mod, "_versions", lambda o: (o.marker,) if isinstance(o, Model) else None)
    model = Model(np.zeros(100_000), uuid.uuid4().hex)
    ex.pin(model)
    try:
        read = lambda: model.marker  # noqa: E731
        assert ex.on("phone").submit(read).result(timeout=30) == 0
        model.marker = 5
        assert ex.on("phone").submit(read).result(timeout=30) == 5
        assert ex.on("mac").submit(read).result(timeout=30) == 5
    finally:
        ex.unpin(model)


def test_kept_arrays_are_read_only(ex):
    x = np.arange(200_000.0)
    ex.pin(x)
    try:
        assert ex.on("phone").submit(lambda: float(x.sum())).result(timeout=30) == float(x.sum())

        def mutate():
            x[0] = 1

        with pytest.raises(ValueError, match="read-only"):
            ex.on("phone").submit(mutate).result(timeout=30)
    finally:
        ex.unpin(x)
    assert x[0] == 0


def test_kept_object_comes_back_after_an_app_restart(ex):
    tag = uuid.uuid4().hex
    model = Model(np.ones(100_000), tag)
    ex.pin(model)
    try:
        f = lambda: (model(np.ones(100_000)), _builds(tag))  # noqa: E731
        assert ex.on("phone").submit(f).result(timeout=30) == (100_000.0, 1)
        old = _phone(ex).id
        src = next(s for s in ex.sources if s.describe() == "simulated phone")
        for p in src.procs:
            p.kill()
        end = time.time() + 30
        while time.time() < end and not any(n.kind == "phone" and n.id != old for n in list(ex.nodes.values())):
            time.sleep(0.1)
        assert ex.on("phone").submit(f).result(timeout=30) == (100_000.0, 1), "rebuilt once on the new app"
    finally:
        ex.unpin(model)


def test_memory_check_counts_the_kept_object(ex):
    model = Model(np.ones(2_000_000), uuid.uuid4().hex)  # 16 MB
    node = _phone(ex)
    node.conn.send({"t": "override_state", "state": {"avail_mem": 10 << 20}})
    try:
        end = time.time() + 5
        while node.state.get("avail_mem") != 10 << 20 and time.time() < end:
            time.sleep(0.05)
        ex.pin(model, on="mac")
        with pytest.raises(PlacementError, match="inputs need"):
            ex.on("phone").submit(lambda: model.tag).result(timeout=30)
    finally:
        node.conn.send({"t": "override_state", "state": {}})
        ex.unpin(model)
        time.sleep(0.3)


def test_older_app_without_kept_objects(ex):
    model = Model(np.ones(100_000), uuid.uuid4().hex)
    node = _phone(ex)
    features = node.hello.pop("features")
    ex.pin(model, on="mac")
    try:
        with pytest.raises(PlacementError, match="older than this pocketnode"):
            ex.on("phone").submit(lambda: model.tag).result(timeout=30)
        f = ex.submit(lambda: model.tag)
        assert f.result(timeout=30) == model.tag and f.placement.kind == "mac"
    finally:
        node.hello["features"] = features
        ex.unpin(model)
