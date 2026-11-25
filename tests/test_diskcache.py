"""Large inputs kept on the phone's disk: they survive an app restart without crossing the cable again."""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid

import numpy as np
import pytest

from test_core import ROOT, SIM_PY, store_args  # noqa: F401  (also keeps the test home private)

import pocketnode as pn
from pocketnode.sources import Address
from pocketnode.store import DISK_MIN, BufferStore
from pocketnode.wire import digest


def _wait(cond, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------- the store itself

def test_disk_tier_keeps_large_buffers_and_reads_them_back(tmp_path):
    big = bytearray(os.urandom(DISK_MIN))
    small = bytearray(os.urandom(1 << 20))
    hb, hs = digest(big), digest(small)
    s = BufferStore(1 << 30, disk_dir=tmp_path)
    s.reserve((), {hb: big, hs: small})
    s.flush()
    assert (tmp_path / hb).exists() and not (tmp_path / hs).exists()
    s.clear()
    assert s.take_evicted() == [hs], "evicting from memory keeps the file, so only the small one is gone"
    assert s.size == 0
    assert s.reserve([hb, hs]) == [hs]
    assert s.size == len(big), "a buffer read back from disk counts against the memory budget"
    assert bytes(s.get([hb])[0]) == bytes(big)
    s.release([hb])
    # A restarted worker finds it, most recently used first.
    again = BufferStore(1 << 30, disk_dir=tmp_path)
    assert again.cached() == [hb]
    assert again.reserve([hb]) == []


def test_damaged_file_is_dropped(tmp_path):
    big = bytearray(os.urandom(DISK_MIN))
    h = digest(big)
    s = BufferStore(1 << 30, disk_dir=tmp_path)
    s.put(h, big)
    s.flush()
    with open(tmp_path / h, "r+b") as f:
        f.seek(12345)
        f.write(bytes([big[12345] ^ 1]))
    again = BufferStore(1 << 30, disk_dir=tmp_path)
    assert again.reserve([h]) == [h]
    assert not (tmp_path / h).exists() and again.take_evicted() == [h]


def test_disk_budget_evicts_least_recently_used(tmp_path):
    bufs = [bytearray(os.urandom(DISK_MIN)) for _ in range(3)]
    hs = [digest(b) for b in bufs]
    s = BufferStore(1 << 30, disk_dir=tmp_path, disk_budget=int(2.5 * DISK_MIN))
    s.put(hs[0], bufs[0])
    s.put(hs[1], bufs[1])
    s.flush()
    s.reserve([hs[0]])  # used again: now the newest
    s.release([hs[0]])
    s.put(hs[2], bufs[2])
    s.flush()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([hs[0], hs[2]])
    assert s.disk_size <= s.disk_budget
    assert hs[1] not in s.take_evicted(), "still in memory, so the Mac can keep counting on it"
    # Order survives a restart: access times are kept on the files.
    os.utime(tmp_path / hs[0], (time.time() - 100, time.time() - 100))
    assert BufferStore(1 << 30, disk_dir=tmp_path).cached() == [hs[2], hs[0]]


def test_pinned_files_are_kept_and_small_pins_go_to_disk(tmp_path):
    small = bytearray(os.urandom(1 << 20))
    big = bytearray(os.urandom(DISK_MIN))
    s = BufferStore(1 << 30, disk_dir=tmp_path, disk_budget=DISK_MIN + (1 << 20))
    s.put(digest(small), small)
    assert s.pin(digest(small))
    s.put(digest(big), big)
    s.flush()
    assert (tmp_path / digest(small)).exists(), "pinned data is on disk whatever its size"
    s.put(digest(other := bytearray(os.urandom(DISK_MIN))), other)
    s.flush()
    assert (tmp_path / digest(small)).exists(), "the disk budget never evicts a pinned file"
    again = BufferStore(1 << 30, disk_dir=tmp_path)
    assert again.pin(digest(small)) and digest(small) in again


# ----------------------------------------------------- against the stand-in

def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Phone:
    """A stand-in phone whose app can be restarted with the same cache directory."""

    def __init__(self, tmp_path):
        if not SIM_PY.exists():
            pytest.skip("no phone stand-in env (.phone-sim)")
        self.port, self.cache, self.tmp = _port(), tmp_path / "cache", tmp_path
        (tmp_path / "token").write_text("t0ken")
        self.extra = store_args(tmp_path)
        self.proc = None
        self.start()

    def start(self):
        self.proc = subprocess.Popen(
            [str(SIM_PY), "-m", "pocketnode.worker", "--listen", f"127.0.0.1:{self.port}",
             "--token-file", str(self.tmp / "token"), "--phone", "--kind", "phone", "--name", "disk-test",
             "--cache-dir", str(self.cache), *self.extra],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, stdout=subprocess.DEVNULL)

    def restart(self, ex, between=None):
        old = {n.id for n in ex.nodes.values()}
        self.proc.kill()
        self.proc.wait()
        if between:
            between()
        self.start()
        assert _wait(lambda: any(n.kind == "phone" and n.id not in old for n in list(ex.nodes.values())), 30)
        return self.node(ex)

    def node(self, ex):
        return next(n for n in list(ex.nodes.values()) if n.kind == "phone")

    def file(self, h):
        return self.cache / "buffers" / h


@pytest.fixture
def phone_ex(tmp_path):
    ph = _Phone(tmp_path)
    ex = pn.PocketExecutor(notes=False)
    src = Address(f"127.0.0.1:{ph.port}", token="t0ken", retry=0.3)
    ex.add_source(src)
    try:
        assert ex.wait_for("phone", 20)
        yield ph, ex
    finally:
        src.stop()
        ex.shutdown(wait=False)
        ph.proc.kill()


def test_input_survives_an_app_restart(phone_ex):
    ph, ex = phone_ex
    x = np.random.default_rng().random(2_000_000)  # 16 MB, never seen before
    h = digest(x)
    assert ex.on("phone").submit(lambda a: float(a.sum()), x).result(timeout=30) == float(x.sum())
    assert _wait(lambda: ph.file(h).exists())
    node = ph.restart(ex)
    assert h in node.known, "the restarted phone said what its disk holds"
    b0 = node.bytes_sent
    assert ex.on("phone").submit(lambda a: float(a.max()), x).result(timeout=30) == float(x.max())
    assert node.bytes_sent - b0 < x.nbytes / 100, f"sent {node.bytes_sent - b0} bytes after the restart"


def test_damaged_file_is_fetched_again(phone_ex):
    ph, ex = phone_ex
    x = np.random.default_rng().random(2_000_000)
    h = digest(x)
    assert ex.on("phone").submit(lambda a: float(a.sum()), x).result(timeout=30) == float(x.sum())
    assert _wait(lambda: ph.file(h).exists())

    def damage():
        with open(ph.file(h), "r+b") as f:
            f.seek(1000)
            b = f.read(1)
            f.seek(1000)
            f.write(bytes([b[0] ^ 0xFF]))

    node = ph.restart(ex, between=damage)
    b0 = node.conn.bytes_out
    assert ex.on("phone").submit(lambda a: float(a.min()), x).result(timeout=30) == float(x.min())
    assert node.conn.bytes_out - b0 >= x.nbytes, "the damaged copy was refused and the data sent again"
    assert _wait(lambda: ph.file(h).exists() and digest(ph.file(h).read_bytes()) == h)


def test_pinned_data_is_repinned_from_disk(phone_ex):
    ph, ex = phone_ex
    x = np.random.default_rng().random(1 << 17)  # 1 MB: below the size that goes to disk unpinned
    h = digest(x)
    ex.pin(x)
    assert _wait(lambda: ph.file(h).exists())
    node = ph.restart(ex)
    assert ex.on("phone").submit(lambda a: float(a.sum()), x).result(timeout=30) == float(x.sum())
    assert node.conn.bytes_out < x.nbytes / 10, f"sent {node.conn.bytes_out} bytes to the restarted phone"
    req = uuid.uuid4().hex
    reply = ex.expect(req)
    node.post({"t": "pin", "req": req, "hashes": [h]})
    assert h in reply(10)["hashes"], "pinned again on the phone"
    ex.unpin(x)
