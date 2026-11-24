"""The App Store edition: pairing at run time, the store worker, the host API, and the
Mac side's handling of a phone whose package set is fixed."""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("POCKETNODE_HOME", tempfile.mkdtemp(prefix="pn-test-home-"))

import pocketnode as pn  # noqa: E402
from pocketnode import pairing, router, sources  # noqa: E402
from pocketnode.errors import PlacementError  # noqa: E402
from pocketnode.pairing import FileBackend, PairingFailed, PairingWindow, PairStore  # noqa: E402
from pocketnode.wire import Connection, client_handshake, server_handshake  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SIM_PY = ROOT / ".phone-sim" / "bin" / "python"


@pytest.fixture(autouse=True)
def mac_home(tmp_path, monkeypatch):
    """This Mac's credentials file lives in a private folder per test."""
    h = tmp_path / "mac-home"
    h.mkdir()
    monkeypatch.setattr(sources, "CONFIG_DIR", h)
    return h


@pytest.fixture(scope="module", autouse=True)
def _restore_process():
    """In-process workers replace sys.stdout/err and add an import hook; put them back afterwards."""
    out, err, meta = sys.stdout, sys.stderr, list(sys.meta_path)
    yield
    sys.stdout, sys.stderr = out, err
    sys.meta_path[:] = meta
    from pocketnode import worker
    for s in (worker._out, worker._err):
        if s is not None:
            s.real = None  # pytest closes its capture files; nothing may flush into them later


# ------------------------------------------------------------------ helpers

def _phone_end(store: PairStore, window: PairingWindow, local: bool = True, name: str = "Test iPhone"):
    """A socketpair whose far end runs the phone's side of one connection. Returns (mac_sock, errors)."""
    a, b = socket.socketpair()
    errors: list = []

    def serve():
        conn = Connection(a, "mac")
        try:
            server_handshake(conn, "", {"name": name, "pairing": "runtime", "phone_id": store.phone_id},
                             token_for=lambda m: store.token(m.get("pair_id")),
                             on_pair=lambda c, m, n: window.handle(c, m, n, store, local, phone_name=name))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return b, errors


def _answer(window: PairingWindow, allow: bool, timeout: float = 10) -> threading.Thread:
    """Tap Allow or Don't Allow once a request is on the phone's screen."""
    def go():
        end = time.time() + timeout
        while time.time() < end:
            st = window.state()
            if st and st["request"]:
                window.decide(allow)
                return
            time.sleep(0.02)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def _store(tmp_path, name="pairs.json") -> PairStore:
    return PairStore(FileBackend(tmp_path / name))


def _pair(store, window, code, mac="My Mac", local=True):
    sock, _ = _phone_end(store, window, local=local)
    return pairing.pair_over(sock, code, mac)


# ------------------------------------------------------------------ pairing

def test_pairing_over_a_socketpair(tmp_path, mac_home):
    store, window = _store(tmp_path), PairingWindow()
    code = window.begin()
    assert window.state()["code"] == code and window.state()["request"] is None
    _answer(window, True)
    r = _pair(store, window, code)
    assert r == {"phone_id": store.phone_id, "phone_name": "Test iPhone"}

    creds = json.loads((mac_home / "paired.json").read_text())
    c = creds[store.phone_id]
    assert stat.S_IMODE((mac_home / "paired.json").stat().st_mode) == 0o600
    assert store.token(c["pair_id"]) == c["token"] and len(c["token"]) == 64
    [entry] = store.list()
    assert entry["name"] == "My Mac" and "token" not in entry
    assert stat.S_IMODE((tmp_path / "pairs.json").stat().st_mode) == 0o600
    assert window.state() is None, "a window is single use"

    # The saved credentials open a session.
    sock, errors = _phone_end(store, window)
    hello = client_handshake(Connection(sock, "phone"), "", {}, credentials=pairing.credentials_for)
    assert hello["phone_id"] == store.phone_id and not errors


def test_phone_proof_is_checked(tmp_path, mac_home):
    """A phone that doesn't know the code can't hand this Mac a token."""
    a, b = socket.socketpair()

    def fake_phone():
        conn = Connection(a, "mac")
        conn.send({"t": "hello", "nonce": "n0", "pairing": "runtime", "phone_id": "abc"})
        conn.recv()
        conn.send({"t": "pair_ok", "pair_id": "p", "token": "t" * 64, "phone_id": "abc",
                   "phone_name": "Impostor", "proof": "0" * 64})

    threading.Thread(target=fake_phone, daemon=True).start()
    with pytest.raises(PairingFailed) as ei:
        pairing.pair_over(b, "123456", "My Mac")
    assert ei.value.why == "proof"
    assert pairing.load() == {}


def test_wrong_code(tmp_path):
    store, window = _store(tmp_path), PairingWindow()
    code = window.begin()
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    with pytest.raises(PairingFailed) as ei:
        _pair(store, window, wrong)
    assert ei.value.why == "code" and store.list() == []
    assert window.state() is not None, "one wrong code doesn't end the attempt"


def test_five_wrong_codes_close_the_window(tmp_path):
    store, window = _store(tmp_path), PairingWindow()
    code = window.begin()
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    for _ in range(5):
        with pytest.raises(PairingFailed, match="doesn't match"):
            _pair(store, window, wrong)
    assert window.state() is None
    with pytest.raises(PairingFailed) as ei:
        _pair(store, window, code)
    assert ei.value.why == "not_pairing"


def test_expired_window(tmp_path):
    store, window = _store(tmp_path), PairingWindow()
    code = window.begin(ttl=0.2)
    time.sleep(0.3)
    assert window.state() is None
    with pytest.raises(PairingFailed) as ei:
        _pair(store, window, code)
    assert ei.value.why == "not_pairing"


def test_no_window(tmp_path):
    store, window = _store(tmp_path), PairingWindow()
    with pytest.raises(PairingFailed) as ei:
        _pair(store, window, "123456")
    assert ei.value.why == "not_pairing" and "Pair a Mac" in str(ei.value)


def test_denied(tmp_path):
    store, window = _store(tmp_path), PairingWindow()
    code = window.begin()
    _answer(window, False)
    with pytest.raises(PairingFailed) as ei:
        _pair(store, window, code)
    assert ei.value.why == "denied" and store.list() == [] and pairing.load() == {}
    assert window.state() is None


def test_decision_timeout(tmp_path):
    store, window = _store(tmp_path), PairingWindow(decide_timeout=0.3)
    code = window.begin()
    with pytest.raises(PairingFailed) as ei:
        _pair(store, window, code)
    assert ei.value.why == "timeout" and store.list() == []


def test_one_request_at_a_time(tmp_path, mac_home):
    store, window = _store(tmp_path), PairingWindow()
    code = window.begin()
    first: dict = {}

    def pair_first():
        try:
            first["r"] = _pair(store, window, code, "First Mac")
        except Exception as e:  # noqa: BLE001
            first["e"] = e

    t = threading.Thread(target=pair_first)
    t.start()
    end = time.time() + 10
    while not (window.state() or {}).get("request") and time.time() < end:
        time.sleep(0.02)
    assert window.state()["request"] == {"name": "First Mac"}
    with pytest.raises(PairingFailed) as ei:
        _pair(store, window, code, "Second Mac")
    assert ei.value.why == "not_pairing"
    window.decide(True)
    t.join(10)
    assert "r" in first and [m["name"] for m in store.list()] == ["First Mac"]


def test_a_second_mac_pairs_too(tmp_path, monkeypatch):
    store = _store(tmp_path)
    homes = []
    for mac in ("Mac A", "Mac B"):
        home = tmp_path / mac.replace(" ", "")
        home.mkdir()
        monkeypatch.setattr(sources, "CONFIG_DIR", home)
        window = PairingWindow()
        code = window.begin()
        _answer(window, True)
        _pair(store, window, code, mac)
        homes.append(home)
    assert sorted(m["name"] for m in store.list()) == ["Mac A", "Mac B"]
    tokens = set()
    for home in homes:
        monkeypatch.setattr(sources, "CONFIG_DIR", home)
        tokens.add(pairing.load()[store.phone_id]["token"])
        sock, errors = _phone_end(store, PairingWindow())
        client_handshake(Connection(sock, "phone"), "", {})  # pairing.credentials_for by default
        assert not errors
    assert len(tokens) == 2


def test_dev_handshake_is_unchanged():
    """The sideloaded edition's handshake: same token both sides, no pairing fields."""
    for token, ok in (("t0ken", True), ("other", False)):
        a, b = socket.socketpair()
        got: dict = {}

        def serve():
            try:
                got["info"] = server_handshake(Connection(a, "mac"), "t0ken", {"name": "dev"})
            except Exception as e:  # noqa: BLE001
                got["e"] = e

        t = threading.Thread(target=serve)
        t.start()
        conn = Connection(b, "phone")
        if ok:
            hello = client_handshake(conn, token, {"session": "s"},
                                     credentials=lambda h: pytest.fail("not consulted for a dev phone"))
            assert hello["name"] == "dev" and "pairing" not in hello
        else:
            with pytest.raises(PermissionError) as ei:
                client_handshake(conn, token, {"session": "s"})
            assert not isinstance(ei.value, pairing.PairingRevoked)
        t.join(5)
        assert ("info" in got) == ok
        if ok:
            assert got["info"]["session"] == "s" and "pair_id" not in got["info"]


def test_unpaired_mac_is_told_so(tmp_path):
    store = _store(tmp_path)
    sock, _ = _phone_end(store, PairingWindow())
    with pytest.raises(pairing.NotPaired):
        client_handshake(Connection(sock, "phone"), "", {})


# ------------------------------------------------------- the store worker

@pytest.fixture(scope="module")
def sw(tmp_path_factory):
    """An in-process StoreWorker on loopback, paired with one Mac ("Test Mac")."""
    from pocketnode.storeworker import StoreWorker
    d = tmp_path_factory.mktemp("store-worker")
    pairs = PairStore(FileBackend(d / "pairs.json"))
    pid, tok = pairs.add("Test Mac")
    w = StoreWorker(pairs, slots=2, cache_dir=str(d / "cache"), phone=False, name="Test iPhone",
                    node_kind="phone")
    ready = threading.Event()
    threading.Thread(target=w.serve, kwargs={"listen": ["127.0.0.1:0"], "ready": lambda a: ready.set()},
                     daemon=True).start()
    assert ready.wait(10)
    w.test_cred = (pid, tok)
    yield w
    w.set_wifi(False)


def _cred(w, pid=None, tok=None):
    pid, tok = (pid, tok) if pid else w.test_cred
    return lambda hello: (tok, {"pair_id": pid}) if hello.get("phone_id") == w.pairs.phone_id else None


@pytest.fixture
def store_ex(sw):
    """An executor whose only node is the in-process store worker."""
    ex = pn.PocketExecutor(notes=False, auto_add=False)
    src = sources.Address(f"127.0.0.1:{sw.port}", token="unused", retry=0.3, credentials=_cred(sw))
    ex.add_source(src)
    assert ex.wait_for("phone", 20), src.status
    ex.test_src = src
    yield ex
    src.stop()
    ex.shutdown(wait=False)
    sw.set_accepting(True)


def _wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _sleepy(seconds):
    import time
    for _ in range(int(seconds / 0.05)):
        time.sleep(0.05)
    return "slept"


def labelled_task(x):
    """Something to recognise on the phone's screen."""
    import time
    time.sleep(1.0)
    return x * 2


def test_store_worker_hello(sw, monkeypatch):
    h = sw.hello()
    assert h["edition"] == "store" and h["pairing"] == "runtime" and h["fixed_packages"] is True
    assert h["phone_id"] == sw.pairs.phone_id
    assert h["disk_free"] is None and h["site"] is None
    assert str(sw.site) not in sys.path
    monkeypatch.setattr(sw, "_no_ssl", True)
    assert {"ssl", "_ssl"} <= set(sw.hello()["stdlib_missing"])


def test_install_and_uninstall_refused(sw, store_ex):
    from pocketnode.provision import hot
    from pocketnode.storeworker import FIXED
    node = next(iter(store_ex.nodes.values()))
    for msg in ({"t": "install", "seq": 0, "last": True, "dists": {}, "files": []},
                {"t": "uninstall", "dists": ["numpy"]}):
        req = f"r-{msg['t']}"
        reply = store_ex.expect(req)
        node.post({**msg, "req": req})
        got = reply(10)
        assert got is not None and got["error"] == FIXED
    node.post({"t": "restart"})  # ignored
    time.sleep(0.3)
    assert node.connected
    with pytest.raises(hot.CantAdd, match="App Store edition"):
        hot.add(store_ex, ["PyYAML"])


def test_label_and_source_reach_the_phone(sw, store_ex):
    f = store_ex.on("phone").submit(labelled_task, 21)
    assert _wait(lambda: sw.tasks_info())
    [t] = sw.tasks_info()
    assert "labelled_task" in t["label"] and t["mac"] == "Test Mac" and t["where"] in ("main", "sub")
    assert isinstance(t["elapsed"], float)
    assert "def labelled_task" in sw.task_source(t["id"])
    [s] = sw.sessions_info()
    assert s["mac"] == "Test Mac" and s["link"] == "usb" and s["pair_id"] == sw.test_cred[0]
    assert f.result(timeout=20) == 42
    assert _wait(lambda: not sw.tasks_info())


def test_stop_tasks(sw, store_ex):
    f = store_ex.on("phone").submit(_sleepy, 30)
    assert _wait(lambda: sw.running)
    assert sw.stop_tasks() == 1
    with pytest.raises(PlacementError, match="by its owner"):
        f.result(timeout=10)
    assert _wait(lambda: not sw.inflight)
    assert store_ex.nodes, "stopping tasks keeps the connection"


def test_accepting_off_refuses_and_cancels(sw, store_ex):
    from pocketnode.storeworker import PAUSED
    f = store_ex.on("phone").submit(_sleepy, 30)
    assert _wait(lambda: sw.running)
    sw.set_accepting(False)
    assert _wait(lambda: not sw.inflight), "the running task was cancelled"
    with pytest.raises(PlacementError, match="by its owner"):
        f.result(timeout=10)
    assert _wait(lambda: not store_ex.nodes)
    assert _wait(lambda: store_ex.test_src.status == PAUSED), store_ex.test_src.status
    sw.set_accepting(True)
    assert store_ex.wait_for("phone", 20)
    assert store_ex.on("phone").submit(_sleepy, 0.1).result(timeout=30) == "slept"


def test_forget_revokes_and_closes(sw, tmp_path):
    pid, tok = sw.pairs.add("Doomed Mac")
    ex = pn.PocketExecutor(notes=False, auto_add=False)
    src = sources.Address(f"127.0.0.1:{sw.port}", token="unused", retry=0.3, credentials=_cred(sw, pid, tok))
    ex.add_source(src)
    try:
        assert ex.wait_for("phone", 20)
        assert any(s["mac"] == "Doomed Mac" for s in sw.sessions_info())
        assert sw.forget(pid)
        assert _wait(lambda: not ex.nodes), "the forgotten Mac's session was closed"
        assert not any(s["mac"] == "Doomed Mac" for s in sw.sessions_info())
        assert _wait(lambda: src.status == sources.REVOKED), src.status
        s = socket.create_connection(("127.0.0.1", sw.port))
        with pytest.raises(pairing.PairingRevoked):
            client_handshake(Connection(s, "phone"), "", {}, credentials=_cred(sw, pid, tok))
    finally:
        src.stop()
        ex.shutdown(wait=False)


def test_non_local_peer_cannot_pair(sw, monkeypatch, mac_home):
    from pocketnode.storeworker import StoreWorker
    code = sw.window.begin()
    try:
        monkeypatch.setattr(StoreWorker, "_peer_is_local", staticmethod(lambda sock: False))
        with pytest.raises(PairingFailed) as ei:
            pairing.pair_phone(address=f"127.0.0.1:{sw.port}", code=code, name="Wi-Fi Mac")
        assert ei.value.why == "usb_only"
        monkeypatch.undo()
        _answer(sw.window, True)
        r = pairing.pair_phone(address=f"127.0.0.1:{sw.port}", code=code, name="Cable Mac")
        assert r["phone_name"] == "Test iPhone"
        assert "Cable Mac" in [m["name"] for m in sw.pairs.list()]
    finally:
        sw.window.cancel()


def test_peer_locality():
    from pocketnode.storeworker import StoreWorker
    a, b = socket.socketpair()
    assert StoreWorker._peer_is_local(a)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.create_connection(srv.getsockname())
    s, _ = srv.accept()
    assert StoreWorker._peer_is_local(s)
    for x in (a, b, srv, c, s):
        x.close()


# ------------------------------------------------------- Mac side: routing

def _fake_node(packages, fixed=True, name="Test iPhone"):
    return SimpleNamespace(kind="phone", same_env=False, name=name, mac_only_modules={},
                           hello={"packages": packages, **({"fixed_packages": True} if fixed else {})},
                           packages={router._norm(k): v for k, v in packages.items()})


def test_release_version_policy(monkeypatch):
    from pocketnode import envinfo
    monkeypatch.setattr(envinfo, "manifest", lambda: {"numpy": {"kind": "compiled", "dist": "numpy",
                                                                "version": "2.5.2"}})
    assert router.check({"numpy"}, _fake_node({"numpy": "2.5.2.post1"})).ok
    v = router.check({"numpy"}, _fake_node({"numpy": "2.5.4"}))
    assert not v.ok and v.reason == "numpy is 2.5.4 on Test iPhone but 2.5.2 on the Mac"
    assert v.suggestion == ("install numpy==2.5.4 on the Mac to use Test iPhone for this "
                            "(its package set is fixed)")
    assert router.check({"numpy"}, _fake_node({"numpy": "2.5.4"}), "minor").ok  # explicit wins
    # Ordinary (sideloaded) phones keep the minor policy.
    assert router.check({"numpy"}, _fake_node({"numpy": "2.5.4"}, fixed=False)).ok
    assert not router.check({"numpy"}, _fake_node({"numpy": "2.6.0"}, fixed=False)).ok
    # Missing from a fixed set: no `packages add` suggestion.
    v = router.check({"numpy"}, _fake_node({}))
    assert not v.ok and v.suggestion is None and v.missing == "numpy"
    assert "packages add" in router.check({"numpy"}, _fake_node({}, fixed=False)).suggestion
    assert router.release("2.5") == "2.5.0" and router.release("1.0.0rc1") == "1.0.0"


def test_fixed_set_note_and_no_adding(monkeypatch):
    from pocketnode import executor as ex_mod
    said = []
    monkeypatch.setattr(ex_mod.log, "info", lambda text, *a: said.append(text))
    ex = pn.PocketExecutor(notes=True, auto_add=True)
    try:
        node = _fake_node({"numpy": "2.5.4"})
        task = SimpleNamespace(fn_label="f")
        ex._missing("lightgbm", task, node)
        ex._missing("lightgbm", task, node)
        assert said == ["pocketnode: lightgbm isn't in Test iPhone's package set (App Store edition: the set "
                        "is fixed when the app is built), so tasks that need it run on the Mac"]
        assert "lightgbm" not in ex._adding and ex.missing_packages["lightgbm"] == 2
    finally:
        ex.shutdown(wait=False)


def test_packages_commands_refuse_on_a_fixed_phone(sw, store_ex, capsys):
    from pocketnode.provision import packages
    from pocketnode.provision.hot import CantAdd
    for call in (lambda: packages.packages.add("PyYAML", executor=store_ex),
                 lambda: packages.packages.remove("numpy", executor=store_ex)):
        with pytest.raises(CantAdd) as ei:
            call()
        assert str(ei.value).startswith("this iPhone runs the App Store edition of pocketnode: its compiled "
                                        "packages are fixed when the app is built (it has: ")
    packages.packages.select(executor=store_ex)
    assert "App Store edition" in capsys.readouterr().out


# ------------------------------------------------------------- the host API

@pytest.fixture
def host(tmp_path):
    from pocketnode import storehost
    storehost.configure(tmp_path / "support", tmp_path / "caches", backend=FileBackend(tmp_path / "pairs.json"),
                        build={"edition": "store", "tls": False, "privacy_url": "https://example.org/privacy",
                               "debug": False}, port=0, phone=False, name="Host iPhone")
    w = storehost.worker
    threading.Thread(target=storehost.serve, daemon=True).start()
    assert _wait(lambda: w.ready)
    yield storehost
    w.set_wifi(False)
    w.set_accepting(False)


def test_status_json_keys_and_types(host):
    st = json.loads(host.status_json())
    types = {"ready": bool, "accepting": bool, "port": int, "wifi": bool, "keep_awake": bool, "sessions": list,
             "paired": list, "tasks": list, "done": int, "thermal": str, "low_power": bool, "cache_bytes": int,
             "background": str, "packages": dict, "python": str, "version": str, "tls": bool, "notes": list,
             "review": bool, "waiting": list, "code_count": int, "fetched_count": int}
    for k, t in types.items():
        assert isinstance(st[k], t), (k, st[k])
    for k in ("wifi_address", "avail_mem", "pairing", "privacy_url"):
        assert k in st
    assert st["ready"] and st["accepting"] and st["port"] > 0 and st["pairing"] is None
    assert st["tls"] is False and st["privacy_url"] == "https://example.org/privacy"
    assert st["background"] == "off" and st["python"] == ".".join(map(str, sys.version_info[:3]))
    assert "numpy" in st["packages"] and "cloudpickle" not in st["packages"]  # compiled ones only
    assert host.tasks_done() == "0"
    snippets = json.loads(host.demo_snippets())
    assert 3 <= len(snippets) <= 4 and all(set(s) == {"title", "code"} for s in snippets)


def test_pairing_through_the_host(host, mac_home):
    code = host.pair_begin()
    assert len(code) == 6 and json.loads(host.status_json())["pairing"]["code"] == code
    got: dict = {}
    t = threading.Thread(target=lambda: got.setdefault(
        "r", pairing.pair_phone(address=f"127.0.0.1:{host.worker.port}", code=code, name="Host Mac")))
    t.start()
    assert _wait(lambda: (json.loads(host.status_json())["pairing"] or {}).get("request"))
    assert json.loads(host.status_json())["pairing"]["request"] == {"name": "Host Mac"}
    host.pair_decide("1")
    t.join(10)
    assert got["r"]["phone_name"] == "Host iPhone"
    st = json.loads(host.status_json())
    assert [p["name"] for p in st["paired"]] == ["Host Mac"] and st["pairing"] is None
    assert set(st["paired"][0]) == {"pair_id", "name", "paired_at", "last_seen"}

    # A session from that Mac shows up, and forgetting the Mac ends it.
    ex = pn.PocketExecutor(notes=False)
    src = sources.Address(f"127.0.0.1:{host.worker.port}", token="unused", retry=0.3)
    ex.add_source(src)
    try:
        assert ex.wait_for("phone", 20), src.status
        assert _wait(lambda: json.loads(host.status_json())["sessions"])
        [s] = json.loads(host.status_json())["sessions"]
        assert s["mac"] == "Host Mac" and s["link"] == "usb"

        # The code it sends is visible, and with review on it waits for Allow.
        assert ex.on("phone").submit(labelled_task, 2).result(timeout=20) == 4
        [row] = [r for r in json.loads(host.code_list()) if "labelled_task" in r["label"]]
        assert set(row) == {"code", "label", "origin", "dist", "where", "srckind", "runs", "last", "state", "macs"}
        assert row["macs"] == ["Host Mac"] and row["state"] == "not needed" and row["runs"] == 1
        assert "def labelled_task" in host.code_source(row["code"])
        host.set_review("1")
        f = ex.on("phone").submit(reviewed_by_host, 3)
        assert _wait(lambda: json.loads(host.status_json())["waiting"])
        [w] = json.loads(host.status_json())["waiting"]
        assert set(w) == {"code", "label", "mac", "pair_id", "where"} and w["mac"] == "Host Mac"
        assert "reviewed_by_host" in w["label"]
        assert host.approve(f"{w['code']} 1") == ""
        assert f.result(timeout=20) == 9
        assert json.loads(host.status_json())["waiting"] == []
        assert json.loads((host._settings_file.parent / "approvals.json").read_text()) == \
            {f"{s['pair_id']}:{w['code']}": "allowed"}
        host.set_review("0")

        assert host.forget(s["pair_id"]) == "1"
        assert _wait(lambda: not ex.nodes)
        assert json.loads(host.status_json())["paired"] == []
    finally:
        src.stop()
        ex.shutdown(wait=False)
    code = host.pair_begin()
    host.pair_cancel()
    assert json.loads(host.status_json())["pairing"] is None


def _run(host, code, timeout=20):
    job = host.run_local(code)
    assert _wait(lambda: json.loads(host.local_poll(job))["done"], timeout)
    return json.loads(host.local_poll(job))


def test_run_local(host):
    r = _run(host, "print(6 * 7)")
    assert r["out"] == "42\n" and r["error"] is None and r["seconds"] >= 0
    r = _run(host, "x = 1\nprint('before')\n1 / x0")
    assert r["out"] == "before\n" and "NameError" in r["error"] and "line 3" in r["error"]
    r = _run(host, "print('x' * 300_000)")
    assert len(r["out"]) < 200_100 and r["out"].endswith("[output cut at 200 KB]\n")
    job = host.run_local("import time\nprint('started', flush=True)\nwhile True:\n    time.sleep(0.01)")
    assert _wait(lambda: "started" in json.loads(host.local_poll(job))["out"])
    host.local_cancel(job)
    assert _wait(lambda: json.loads(host.local_poll(job))["done"])
    assert json.loads(host.local_poll(job))["error"] == "stopped"
    for s in json.loads(host.demo_snippets()):
        r = _run(host, s["code"], timeout=60)
        assert r["error"] is None and r["out"].strip(), (s["title"], r)


def test_settings_persist(host, tmp_path):
    host.set_keep_awake("1")
    host.set_accepting("0")
    assert host.set_wifi("1") == ""
    st = json.loads(host.status_json())
    assert st["keep_awake"] and not st["accepting"] and st["wifi"]
    assert st["wifi_address"] is None or st["wifi_address"].endswith(f":{st['port']}")
    host.set_wifi("0")
    assert not json.loads(host.status_json())["wifi"]
    host.set_wifi("1")
    # A fresh start reads them back.
    from pocketnode import storehost
    w = storehost.configure(tmp_path / "support", tmp_path / "caches", backend=FileBackend(tmp_path / "pairs.json"),
                            port=0, phone=False)
    st = json.loads(storehost.status_json())
    assert st["keep_awake"] and not st["accepting"] and not w.accepting and w._wifi_wanted
    assert json.loads((tmp_path / "support" / "store-settings.json").read_text()) == \
        {"accepting": False, "wifi": True, "keep_awake": True, "review": False}


def test_ios_entry_point_delegates_in_the_store_edition(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "src" / "pocketnode" / "ios" / "App" / "python"))
    try:
        import pocketnode_ios
        from pocketnode import storehost
        monkeypatch.setattr(pocketnode_ios, "_settings", lambda: {"edition": "store"})
        monkeypatch.setattr(storehost, "worker", None)
        assert json.loads(pocketnode_ios.status()) == {"ready": False}
        assert pocketnode_ios.demo_snippets is storehost.demo_snippets
        assert pocketnode_ios.tasks_done() == 0
        monkeypatch.setattr(pocketnode_ios, "_settings", lambda: {})
        assert pocketnode_ios.status() == "starting…"
        with pytest.raises(AttributeError):
            pocketnode_ios.demo_snippets  # noqa: B018
    finally:
        sys.path.remove(str(ROOT / "src" / "pocketnode" / "ios" / "App" / "python"))


# ------------------------------------------------------------ the stand-in

def test_simulated_store_phone_end_to_end(tmp_path):
    if not SIM_PY.exists():
        pytest.skip("no phone stand-in env (.phone-sim)")
    src = sources.SimulatedPhone(python=str(SIM_PY), edition="store", slots=2)
    ex = pn.PocketExecutor(notes=False, auto_add=False)
    ex.add_source(src)
    try:
        assert ex.wait_for("phone", 30)
        node = next(iter(ex.nodes.values()))
        assert node.hello["edition"] == "store" and node.hello["fixed_packages"]
        assert ex.on("phone").submit(labelled_task, 5).result(timeout=30) == 10

        def uses_yaml():
            import yaml
            return yaml.__version__

        pytest.importorskip("yaml")
        with pytest.raises(PlacementError, match="PyYAML") as ei:
            ex.on("phone").submit(uses_yaml).result(timeout=30)
        assert "packages add" not in str(ei.value)
    finally:
        ex.shutdown(wait=False)


# ------------------------------------------------- round 2: code made visible

def reviewed_by_host(x):
    return x * x


def review_auto(x):
    return x + 100


def review_forced(x):
    return x + 200


def review_denied(x):
    return x + 300


def review_timeout(x):
    return x + 400


def review_off(x):
    return x + 500


def test_code_info_for_functions_lambdas_and_libraries():
    import functools

    import numpy as np

    from pocketnode.executor import SRC_LIMIT, _code_info
    ci = _code_info(labelled_task)
    assert ci["srckind"] == "source" and ci["src"].startswith("def labelled_task")
    assert ci["origin"] == "user" and ci["dist"] is None
    assert ci["where"] == f"test_store.py:{labelled_task.__code__.co_firstlineno}"
    assert len(ci["code"]) == 16 and _code_info(functools.partial(labelled_task, 1))["code"] == ci["code"]
    ns: dict = {}
    exec("f = lambda x: x + 1", ns)  # no file behind it: the disassembly is shown instead
    lam = _code_info(ns["f"], "<lambda>")
    assert lam["srckind"] == "bytecode" and "RETURN" in lam["src"] and lam["origin"] == "user"
    lib = _code_info(np.linalg.norm)
    assert lib["origin"] == "library" and lib["dist"] == f"numpy {np.__version__}" and lib["srckind"] == "source"
    big: dict = {}
    exec("def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(6000)), big)
    assert len(_code_info(big["big"])["src"].encode()) <= SRC_LIMIT


def test_code_log_on_the_phone(sw, store_ex):
    assert store_ex.on("phone").submit(labelled_task, 1).result(timeout=20) == 2
    assert store_ex.on("phone").submit(labelled_task, 2).result(timeout=20) == 4
    [e] = [r for r in sw.code_list() if "labelled_task" in r["label"]]
    assert e["origin"] == "user" and e["srckind"] == "source" and e["macs"] == ["Test Mac"]
    assert e["runs"] >= 2 and e["state"] == "not needed" and e["where"].startswith("test_store.py:")
    assert sw.code_source(e["code"]).startswith("def labelled_task")
    # The read-only diagnostic request answers with the same list and a digest of the text held.
    node = next(iter(store_ex.nodes.values()))
    reply = store_ex.expect("cl")
    node.post({"t": "code_log", "req": "cl"})
    rows = reply(10)["codes"]
    [r] = [r for r in rows if r["code"] == e["code"]]
    import hashlib
    assert r["src_digest"] == hashlib.sha256(sw.code_source(e["code"]).encode()).hexdigest()[:16] == e["code"]


@pytest.fixture
def reviewing(sw):
    sw.set_review(True)
    yield sw
    sw.set_review(False)


def test_review_holds_user_code_until_allowed(reviewing, tmp_path, monkeypatch):
    from pocketnode import executor as ex_mod
    sw = reviewing
    said = []
    monkeypatch.setattr(ex_mod.log, "info", lambda text, *a: said.append(text))
    ex = pn.PocketExecutor(notes=True, auto_add=False, verify=False)
    mac = sources.LocalWorkers(1)
    src = sources.Address(f"127.0.0.1:{sw.port}", token="unused", retry=0.3, credentials=_cred(sw))
    for s in (mac, src):
        ex.add_source(s)
    try:
        assert ex.wait_for("phone", 20) and ex.wait_for("mac", 30)
        phone_node = next(n for n in ex.nodes.values() if n.kind == "phone")
        # Automatic placement: the phone holds it back, the Mac runs it.
        f = ex.submit(review_auto, 1)
        assert f.result(timeout=30) == 101 and f.placement.kind == "mac"
        f = ex.on("phone").submit(review_forced, 1)
        assert _wait(lambda: any("review_forced" in w["label"] for w in sw.waiting_info()))
        code = next(w["code"] for w in sw.waiting_info() if "review_forced" in w["label"])
        assert phone_node.held.get(code) == "waiting"
        assert any("is waiting for approval on Test iPhone (Ask before running new code is on there)" in t
                   for t in said)
        assert not f.done(), "a forced task waits for the answer"
        assert sw.approve(code, True) == 1
        assert f.result(timeout=20) == 201 and f.placement.kind == "phone"
        assert code not in phone_node.held
        assert next(e for e in sw.code_list() if e["code"] == code)["state"] == "allowed"
        assert ex.on("phone").submit(review_forced, 2).result(timeout=20) == 202  # allowed stays allowed
        # Approvals persist: a restarted worker reading the same file has them.
        from pocketnode.storeworker import StoreWorker
        path = tmp_path / "approvals.json"
        sw.set_approvals_file(path)  # (no file yet: the current decisions are kept)
        sw._save_decisions()
        again = StoreWorker.__new__(StoreWorker)
        again._lock, again.decisions = threading.RLock(), {}
        StoreWorker.set_approvals_file(again, path)
        assert again.decisions[f"{sw.test_cred[0]}:{code}"] == "allowed"
    finally:
        sw._approvals_file = None
        for s in (mac, src):
            s.stop()
        ex.shutdown(wait=False)


def test_review_denied_and_library_code(reviewing, store_ex):
    import numpy as np
    sw = reviewing
    f = store_ex.on("phone").submit(review_denied, 1)
    assert _wait(lambda: any("review_denied" in w["label"] for w in sw.waiting_info()))
    code = next(w["code"] for w in sw.waiting_info() if "review_denied" in w["label"])
    sw.approve(code, False)
    with pytest.raises(PlacementError, match="declined"):
        f.result(timeout=20)
    with pytest.raises(PlacementError, match="declined"):  # stays declined
        store_ex.on("phone").submit(review_denied, 2).result(timeout=20)
    # Library code is never held.
    x = np.arange(4.0)
    assert store_ex.on("phone").submit(np.linalg.norm, x).result(timeout=20) == float(np.linalg.norm(x))
    assert next(e for e in sw.code_list() if "norm" in e["label"])["state"] == "not needed"


def test_forced_task_hold_times_out(reviewing, store_ex, monkeypatch):
    from pocketnode import executor as ex_mod
    monkeypatch.setattr(ex_mod, "APPROVAL_WAIT", 0.5)
    with pytest.raises(PlacementError, match="wasn't approved on Test iPhone within"):
        store_ex.on("phone").submit(review_timeout, 1).result(timeout=20)


def test_review_off_runs_without_asking(sw, store_ex):
    assert not sw.review
    assert store_ex.on("phone").submit(review_off, 1).result(timeout=20) == 501
    assert not sw.waiting_info()


def test_older_mac_without_origin_counts_as_user(reviewing):
    """A task header from a Mac package older than the code log: no code, origin or source."""
    sw = reviewing
    conn = Connection(socket.create_connection(("127.0.0.1", sw.port)), "phone")
    client_handshake(conn, "", {"session": "old-mac"}, credentials=_cred(sw))
    try:
        conn.send({"t": "task", "id": "old-1", "bufs": [], "fk": "old.fn:abc()", "label": "fn"}, [b"x"])
        conn.sock.settimeout(10)
        while True:
            msg, _ = conn.recv()
            if msg["t"] == "result":
                break
        assert msg["unavailable"]["kind"] == "approval"
        assert msg["unavailable"]["detail"] == "fn is waiting for approval on Test iPhone"
        [w] = [w for w in sw.waiting_info() if w["label"] == "fn"]
        assert next(e for e in sw.code_list() if e["code"] == w["code"])["origin"] == "user"
    finally:
        conn.close()


def test_bye_is_not_a_crash(sw, store_ex):
    f = store_ex.on("phone").submit(_sleepy, 30)
    assert _wait(lambda: sw.running)
    node = next(iter(store_ex.nodes.values()))
    sw.set_accepting(False)
    with pytest.raises(PlacementError, match="by its owner"):
        f.result(timeout=10)
    assert _wait(lambda: not store_ex.nodes)
    assert node.bye == "paused" and node.lost_as_crash is False


def test_fetched_files(host):
    root = Path(host.worker.finder.cache_dir)
    pkg = root / "mypkg-0123abcd" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("X = 1\n")
    (pkg / "sub.py").write_text("def f():\n    return 2\n")
    (pkg / "data.txt").write_text("not listed")
    (root / "outside-link.py").symlink_to("/etc/hosts")
    rows = json.loads(host.fetched_json())
    assert rows == [{"path": "mypkg-0123abcd/mypkg/__init__.py", "module": "mypkg", "bytes": 6},
                    {"path": "mypkg-0123abcd/mypkg/sub.py", "module": "mypkg.sub", "bytes": 22}]
    assert host.fetched_source("mypkg-0123abcd/mypkg/sub.py") == "def f():\n    return 2\n"
    for bad in ("../../../../etc/hosts", "/etc/hosts", "outside-link.py", "", "mypkg-0123abcd"):
        assert host.fetched_source(bad) == "", bad
    import pocketnode.storehost as sh
    sh._fetched_count = (0.0, 0)
    assert json.loads(host.status_json())["fetched_count"] == 2


def test_review_default_comes_from_the_build(tmp_path):
    from pocketnode import storehost
    storehost.configure(tmp_path / "s", tmp_path / "c", backend=FileBackend(tmp_path / "p.json"),
                        build={"edition": "store", "review": True}, port=0, phone=False)
    assert storehost.status()["review"] is True and storehost.worker.review
    storehost.set_review("0")
    storehost.configure(tmp_path / "s", tmp_path / "c", backend=FileBackend(tmp_path / "p.json"),
                        build={"edition": "store", "review": True}, port=0, phone=False)
    assert storehost.status()["review"] is False  # the saved choice wins over the build's default


def test_dev_token_is_made_only_for_a_dev_phone(sw, mac_home):
    ex = pn.PocketExecutor(notes=False)
    src = sources.Address(f"127.0.0.1:{sw.port}", retry=0.3, credentials=_cred(sw))
    ex.add_source(src)
    try:
        assert ex.wait_for("phone", 20)
        assert not (mac_home / "token").exists(), "a store phone needs no dev token"
    finally:
        src.stop()
        ex.shutdown(wait=False)
    a, b = socket.socketpair()
    threading.Thread(target=lambda: server_handshake(Connection(a, "mac"), "t0ken", {"name": "dev"}),
                     daemon=True).start()
    asked = []
    client_handshake(Connection(b, "phone"), lambda: asked.append(1) or "t0ken", {})
    assert asked == [1]


def test_packages_refused_under_the_store_flavour_without_a_phone(monkeypatch, capsys):
    import contextlib

    from pocketnode.provision import app as app_mod
    from pocketnode.provision import packages
    from pocketnode.provision.hot import CantAdd
    from pocketnode.storeworker import FIXED
    monkeypatch.setattr(pn, "_current", None)
    monkeypatch.setattr(app_mod, "current_flavour", lambda cfg=None: "store")
    monkeypatch.setattr(packages, "_live", contextlib.contextmanager(lambda executor=None: (yield None)))
    monkeypatch.setattr(app_mod, "build", lambda *a, **k: pytest.fail("must not build the dev app"))
    for call in (lambda: packages.packages.add("PyYAML"), lambda: packages.packages.remove("PyYAML"),
                 lambda: packages.packages.add("PyYAML", bundle=True)):
        with pytest.raises(CantAdd) as ei:
            call()
        assert str(ei.value) == FIXED
    packages.packages.select()
    assert FIXED in capsys.readouterr().out


def test_phonelink_never_launches_a_store_app(monkeypatch, mac_home):
    from pocketnode import usbmux
    launches = []
    monkeypatch.setattr(sources.usbmux, "list_devices", lambda: [usbmux.Device(1, "UDID-1", "USB")])

    def refuse(*a):
        raise OSError("not listening")

    monkeypatch.setattr(sources.usbmux, "connect", refuse)
    monkeypatch.setattr(sources, "launch_app", lambda udid, restart=False: launches.append(udid))

    def run(seconds):
        ex = pn.PocketExecutor(notes=False)
        link = sources.PhoneLink()
        ex.add_source(link)
        time.sleep(seconds)
        link.stop()
        ex.shutdown(wait=False)
        return link

    # A Mac that only ever paired with store phones and never built the sideloaded app.
    pairing.remember("phone1", "pair1", "t" * 64, "Store iPhone")
    link = run(3.5)
    assert link.status == sources.OPEN_APP and launches == []
    # The same Mac with a sideloaded app built here keeps relaunching as before.
    (mac_home / "app.json").write_text(json.dumps({"team": "ABCDE12345"}))
    run(3.5)
    assert launches == ["UDID-1"]
    assert not (mac_home / "token").exists()
