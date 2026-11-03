"""The Mac side: a concurrent.futures.Executor over Mac cores and the phone.

Scheduling in one paragraph: every task is packed once (cloudpickle, protocol 5,
content-addressed buffers). Each free slot on each node pulls the next task it
can run. A phone slot only takes a task when its estimated finish time beats the
Mac's, using measured per-function compute times and link bandwidth, so
memory-bound work stays on the Mac and heavy work spreads out. When the queue
drains, idle Mac slots duplicate tasks stuck on a slow, paused, hot or vanished
phone; the first result wins. Errors and platform misses on the phone are
re-run on the Mac, so a result is always what the Mac would have produced.
"""

from __future__ import annotations

import contextvars
import hashlib
import itertools
import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import envinfo, router, serialize
from .errors import NodeLost, PlacementError, RemoteTraceback
from .wire import Connection, ConnectionClosed, client_handshake

log = logging.getLogger("pocketnode")

_placement_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("pocketnode_placement", default=None)

THERMAL_SCALE = {"nominal": 1.0, "fair": 1.0, "serious": 0.5, "critical": 0.0}


# ------------------------------------------------------------------ records

@dataclass
class Placement:
    """Where a task ran and why. Attached to every future as `.placement`."""

    node: str = ""
    kind: str = ""
    reason: str = ""
    suggestion: str | None = None
    attempts: list[str] = field(default_factory=list)
    seconds: float = 0.0
    transfer_bytes: int = 0

    def __str__(self):
        s = f"ran on {self.node} ({self.kind}): {self.reason}"
        if self.suggestion:
            s += f". Tip: {self.suggestion}"
        return s


class PocketFuture(Future):
    placement: Placement

    def __init__(self):
        super().__init__()
        self.placement = Placement()


@dataclass
class Task:
    id: str
    fn_key: str
    fn_label: str
    packed: serialize.Packed
    future: PocketFuture
    placement: str  # auto | mac | phone | <node name>
    ro: bool = False
    submitted: float = field(default_factory=time.monotonic)
    attempts: dict[str, "Attempt"] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)  # node kind/name -> reason
    confirm_error: Any = None  # phone exception awaiting confirmation on the Mac
    queued: bool = True
    reason: str = ""
    suggestion: str | None = None

    @property
    def done(self) -> bool:
        return self.future.done()


@dataclass
class Attempt:
    id: str
    task: Task
    node: "Node"
    started: float
    speculative: bool = False
    est: float | None = None


class FnStats:
    """Measured compute seconds per (function, node kind), EWMA."""

    def __init__(self):
        self.t: dict[tuple[str, str], float] = {}
        self.n: dict[tuple[str, str], int] = defaultdict(int)

    def add(self, key: str, kind: str, secs: float) -> None:
        k = (key, kind)
        self.n[k] += 1
        old = self.t.get(k)
        self.t[k] = secs if old is None else 0.7 * old + 0.3 * secs

    def get(self, key: str, kind: str) -> float | None:
        return self.t.get((key, kind))


# -------------------------------------------------------------------- nodes

class Node:
    def __init__(self, ex: "PocketExecutor", conn: Connection, hello: dict, source: "Source"):
        self.ex = ex
        self.conn = conn
        self.hello = hello
        self.source = source
        self.id = hello["id"]
        self.name = hello["name"]
        self.kind = hello["kind"]
        self.slots = int(hello["slots"])
        self.same_env = source.same_env
        self.state = hello.get("state", {})
        self.known: set[str] = set()
        self.running: dict[str, Attempt] = {}
        self.connected = True
        self.mac_only_modules: dict[str, str] = {}
        self.bandwidth = source.bandwidth_guess  # bytes/s, refined by measurement
        self.latency = source.latency_guess
        self.bytes_sent = 0

    def effective_slots(self) -> int:
        if not self.connected:
            return 0
        if self.state.get("lifecycle") in ("background", "suspending", "inactive-long"):
            return 0
        scale = THERMAL_SCALE.get(self.state.get("thermal", "nominal"), 1.0)
        if scale == 0:
            return 0
        return max(1, int(self.slots * scale))

    def free(self) -> int:
        return self.effective_slots() - len(self.running)

    def transfer_cost(self, task: Task) -> tuple[float, int]:
        new = len(task.packed.data) + sum(
            b.nbytes for b, h in zip(task.packed.buffers, task.packed.hashes) if h not in self.known
        )
        return self.latency + new / self.bandwidth, new

    def label(self) -> str:
        return f"{self.name}" if self.kind == "mac" else f"{self.name} ({self.kind})"

    # ------------------------------------------------------------ sending

    def send_task(self, att: Attempt) -> int:
        t = att.task
        specs, inline = [], []
        for b, h in zip(t.packed.buffers, t.packed.hashes):
            if h in self.known:
                specs.append({"h": h})
            else:
                specs.append({"h": h, "i": 1})
                inline.append(b)
                self.known.add(h)
        header = {"t": "task", "id": att.id, "bufs": specs}
        if t.ro:
            header["ro"] = 1
        self.conn.send(header, [t.packed.data, *inline])
        n = len(t.packed.data) + sum(b.nbytes for b in inline)
        self.bytes_sent += n
        return n

    def reader(self) -> None:
        try:
            while True:
                msg, bufs = self.conn.recv()
                self.ex._on_message(self, msg, bufs)
        except (ConnectionClosed, OSError):
            pass
        except Exception:
            log.exception("pocketnode: error handling message from %s", self.name)
        finally:
            self.ex._on_disconnect(self)


# ------------------------------------------------------------------ sources

class Source:
    """Something that produces nodes: local worker processes, a phone link, an address."""

    same_env = False
    bandwidth_guess = 30e6
    latency_guess = 0.002

    def start(self, ex: "PocketExecutor") -> None:
        raise NotImplementedError

    def stop(self) -> None:
        pass

    def describe(self) -> str:
        return type(self).__name__


def attach(ex: "PocketExecutor", sock, source: Source, token: str) -> Node | None:
    conn = Connection(sock, source.describe())
    hello = client_handshake(conn, token, {
        "session": ex.session,
        "manifest": None if source.same_env else envinfo.manifest(),
    })
    node = Node(ex, conn, hello, source)
    ex._add_node(node)
    threading.Thread(target=node.reader, daemon=True, name=f"pn-read-{node.name}").start()
    return node


# ----------------------------------------------------------------- executor

def _fn_identity(fn) -> tuple[str, str]:
    f = getattr(fn, "func", fn)  # functools.partial
    f = getattr(f, "__func__", f)
    code = getattr(f, "__code__", None)
    label = getattr(f, "__qualname__", None) or type(f).__name__
    if code is not None:
        label = f"{label} ({os.path.basename(code.co_filename)}:{code.co_firstlineno})"
        h = hashlib.sha1(code.co_code + repr(code.co_consts).encode("utf-8", "replace")).hexdigest()[:12]
        return f"{getattr(f, '__module__', '')}.{f.__qualname__}:{h}", label
    return f"{type(f).__module__}.{type(f).__qualname__}", label


class PocketExecutor(Executor):
    def __init__(self, *, placement: str = "auto", strict: bool = False, version_policy: str = "minor",
                 speculate: bool = True, confirm_errors: bool = True, notes: bool = True):
        self.session = uuid.uuid4().hex
        self.default_placement = placement
        self.strict = strict
        self.version_policy = version_policy
        self.speculate = speculate
        self.confirm_errors = confirm_errors
        self.notes = notes
        self.nodes: dict[str, Node] = {}
        self.sources: list[Source] = []
        self.queue: deque[Task] = deque()
        self.tasks: dict[str, Task] = {}
        self.attempts: dict[str, Attempt] = {}
        self.stats = FnStats()
        self.mac_only: dict[str, tuple[str, str | None]] = {}  # fn_key -> (reason, suggestion)
        self.missing_packages: dict[str, int] = defaultdict(int)
        self._cv = threading.Condition()
        self._shutdown = False
        self._noted: set[tuple] = set()
        self._buf_by_hash: dict[str, tuple[memoryview, int]] = {}
        self.history: deque[tuple[str, str, str, str]] = deque(maxlen=5000)
        self._sched = threading.Thread(target=self._loop, daemon=True, name="pn-scheduler")
        self._sched.start()
        serialize.register_user_modules()

    # --------------------------------------------------------------- API

    def add_source(self, src: Source) -> None:
        self.sources.append(src)
        src.start(self)

    def submit(self, fn: Callable, /, *args, **kwargs) -> PocketFuture:
        return self._submit(fn, args, kwargs, None, None)

    def _submit(self, fn, args, kwargs, placement, memo, ro=False) -> PocketFuture:
        if self._shutdown:
            raise RuntimeError("cannot schedule new futures after shutdown")
        serialize.register_user_modules()
        where = (placement or getattr(fn, "__pocketnode_placement__", None)
                 or _placement_ctx.get() or self.default_placement)
        packed = serialize.dumps((fn, args, kwargs), memo=memo)
        key, label = _fn_identity(fn)
        fut = PocketFuture()
        task = Task(uuid.uuid4().hex, key, label, packed, fut, where, ro=ro)
        with self._cv:
            for b, h in zip(packed.buffers, packed.hashes):
                self._buf_by_hash[h] = (b, self._buf_by_hash.get(h, (b, 0))[1] + 1)
            self.tasks[task.id] = task
            self.queue.append(task)
            self._cv.notify_all()
        fut.add_done_callback(lambda f, t=task: self._release(t))
        return fut

    def map(self, fn, *iterables, timeout=None, chunksize=1, placement=None, buffersize=None):
        memo = serialize.HashMemo()
        if chunksize > 1:
            chunks = _chunks(zip(*iterables), chunksize)
            futs = [self._submit(_run_chunk, (fn, c), {}, placement, memo) for c in chunks]
            return _result_iter(futs, timeout, flatten=True)
        futs = [self._submit(fn, a, {}, placement, memo) for a in zip(*iterables)]
        return _result_iter(futs, timeout)

    def on(self, placement: str) -> "PlacedExecutor":
        """A view of this executor that forces placement: `ex.on("phone").submit(f, x)`."""
        return PlacedExecutor(self, placement)

    def pin(self, obj, on: str = "phone"):
        """Keep obj's buffers resident on matching nodes; tasks using obj then prefer them.

        Returns obj unchanged, so you keep using it as usual.
        """
        packed = serialize.dumps(obj, scan=False)
        targets = [n for n in self.nodes.values() if _matches(n, on)]
        if not targets:
            raise PlacementError(f"no connected node matches {on!r}")
        for n in targets:
            new = [(h, b) for h, b in zip(packed.hashes, packed.buffers) if h not in n.known]
            if new:
                n.conn.send({"t": "put", "hashes": [h for h, _ in new]}, [b for _, b in new])
                n.known.update(h for h, _ in new)
            n.conn.send({"t": "pin", "hashes": packed.hashes})
        return obj

    def unpin(self, obj) -> None:
        packed = serialize.dumps(obj, scan=False)
        for n in self.nodes.values():
            n.conn.send({"t": "unpin", "hashes": packed.hashes})

    def wait_for(self, what: str = "phone", timeout: float | None = None) -> bool:
        """Block until a node matching `what` (kind or name) is connected."""
        end = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while not any(_matches(n, what) for n in self.nodes.values()):
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._cv.wait(0.2 if left is None else min(0.2, left))
        return True

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._cv:
            self._shutdown = True
            if cancel_futures:
                for t in list(self.queue):
                    t.future.cancel()
                self.queue.clear()
            self._cv.notify_all()
        if wait:
            for t in list(self.tasks.values()):
                try:
                    t.future.exception()
                except Exception:
                    pass
        for s in self.sources:
            s.stop()
        for n in list(self.nodes.values()):
            n.conn.close()

    # ----------------------------------------------------------- reports

    def describe(self) -> str:
        lines = []
        for n in self.nodes.values():
            st = n.state
            lines.append(
                f"{n.label():32} slots={n.effective_slots()}/{n.slots} running={len(n.running)} "
                f"thermal={st.get('thermal', '?')} {st.get('lifecycle', '')}"
            )
        return "\n".join(lines) or "no nodes connected"

    def explain(self) -> str:
        """Per function: where its tasks ran and why."""
        rows: dict[tuple[str, str], int] = defaultdict(int)
        reasons: dict[str, set[str]] = defaultdict(set)
        for label, node, kind, reason in self.history:
            rows[(label, kind)] += 1
            if reason:
                reasons[label].add(reason)
        out = []
        for (label, kind), n in sorted(rows.items()):
            out.append(f"{label}: {n} on {kind}")
        for label, rs in reasons.items():
            for r in sorted(rs):
                out.append(f"  {label}: {r}")
        return "\n".join(out)

    # --------------------------------------------------------- internals

    def _release(self, task: Task) -> None:
        with self._cv:
            self.tasks.pop(task.id, None)
            for h in task.packed.hashes:
                b, n = self._buf_by_hash.get(h, (None, 0))
                if n <= 1:
                    self._buf_by_hash.pop(h, None)
                else:
                    self._buf_by_hash[h] = (b, n - 1)
            for a in list(task.attempts.values()):
                self.attempts.pop(a.id, None)
                if a.node.running.pop(a.id, None) is not None:
                    try:
                        a.node.conn.send({"t": "cancel", "id": a.id})
                    except ConnectionClosed:
                        pass
            self._cv.notify_all()

    def _add_node(self, node: Node) -> None:
        with self._cv:
            old = [n for n in self.nodes.values() if n.id == node.id]
            for o in old:  # same worker reconnecting: carry over what we know
                node.known = o.known
                node.running = o.running
                for a in node.running.values():
                    a.node = node
            self.nodes[node.id] = node
            self._cv.notify_all()
        if node.kind != "mac":
            self._note(("node", node.name), f"pocketnode: {node.label()} connected "
                       f"({node.slots} slots, Python {'.'.join(map(str, node.hello['python']))})")

    def _on_disconnect(self, node: Node) -> None:
        with self._cv:
            node.connected = False
            if self.nodes.get(node.id) is node:
                del self.nodes[node.id]
            # In-flight work there is now suspect: make it runnable elsewhere.
            # A late result (after reconnect) is still accepted if it comes first.
            for a in list(node.running.values()):
                t = a.task
                if not t.done and not t.queued and not any(
                    x.node.connected and x.id != a.id for x in t.attempts.values()
                ):
                    t.queued = True
                    self.queue.appendleft(t)
            self._cv.notify_all()
        if node.kind != "mac" and not self._shutdown:
            self._note(None, f"pocketnode: lost {node.label()}; its tasks are being re-run on other nodes")

    def _eligible(self, task: Task, node: Node) -> tuple[bool, str]:
        p = task.placement
        if p not in ("auto", None) and not _matches(node, p):
            return False, ""
        if node.kind in task.excluded or node.name in task.excluded:
            return False, task.excluded.get(node.kind) or task.excluded[node.name]
        if node.kind != "mac" and task.fn_key in self.mac_only and p in ("auto", None):
            return False, self.mac_only[task.fn_key][0]
        v = router.check(task.packed.modules, node, self.version_policy)
        if not v.ok:
            if p not in ("auto", None) or self.strict:
                self._fail(task, PlacementError(f"{task.fn_label} can't run on {node.label()}: {v.reason}"
                                                + (f". {v.suggestion}" if v.suggestion else "")))
            else:
                task.excluded[node.kind] = v.reason
                if v.missing:
                    self.missing_packages[v.missing] += 1
                task.suggestion = v.suggestion
            return False, v.reason
        return True, ""

    def _estimate(self, task: Task, node: Node) -> float | None:
        c = self.stats.get(task.fn_key, node.kind)
        if c is None:
            return None
        tc, _ = node.transfer_cost(task)
        return c + tc

    def _mac_finish_estimate(self, task: Task) -> float | None:
        macs = [n for n in self.nodes.values() if n.kind == "mac"]
        c = self.stats.get(task.fn_key, "mac")
        if not macs or c is None:
            return None
        slots = sum(n.effective_slots() for n in macs) or 1
        ahead = sum(1 for t in self.queue if t.queued) / slots
        return (ahead + 1) * c

    def _pick(self, node: Node) -> Task | None:
        """Next queued task this node should run, or None."""
        for task in list(self.queue):
            if task.done or not task.queued:
                continue
            ok, _ = self._eligible(task, node)
            if task.done:
                continue
            if not ok:
                continue
            if node.kind != "mac" and task.placement in ("auto", None):
                mine = self._estimate(task, node)
                mac = self._mac_finish_estimate(task)
                if mine is not None and mac is not None and mine > mac:
                    task.reason = f"Mac was faster ({mac:.2g}s vs {mine:.2g}s estimated on {node.name})"
                    continue
            return task
        return None

    def _orphaned(self) -> bool:
        return any(not t.done and not t.queued and not any(
            a.node.connected for a in t.attempts.values()) for t in self.tasks.values())

    def _loop(self) -> None:
        while True:
            with self._cv:
                if self._shutdown and not self.tasks:
                    return
                launched = self._dispatch_some()
                if not launched:
                    self._cv.wait(0.25)

    def _dispatch_some(self) -> bool:
        launched = False
        # Fastest node kinds first so the Mac fills before the phone gets marginal work.
        nodes = sorted(self.nodes.values(), key=lambda n: (n.kind != "mac", n.name))
        for node in nodes:
            while node.free() > 0:
                task = self._pick(node)
                if task is None:
                    break
                self._launch(task, node, speculative=False)
                launched = True
        while self.queue and (self.queue[0].done or not self.queue[0].queued):
            self.queue.popleft()
        if self.speculate and not any(t.queued and not t.done for t in self.queue):
            launched |= self._speculate()
        return launched

    def _speculate(self) -> bool:
        idle = [n for n in self.nodes.values() if n.kind == "mac" and n.free() > 0]
        if not idle:
            return False
        now = time.monotonic()
        launched = False
        for task in sorted(self.tasks.values(), key=lambda t: t.submitted):
            if task.done or task.queued or len(task.attempts) >= 2:
                continue
            atts = list(task.attempts.values())
            if not atts or any(a.node.kind == "mac" for a in atts):
                continue
            a = atts[0]
            mac_t = self.stats.get(task.fn_key, "mac")
            stuck = (not a.node.connected or a.node.effective_slots() == 0
                     or (mac_t is not None and now - a.started > mac_t * 1.5)
                     or (mac_t is None and now - a.started > 2.0))
            if not stuck:
                continue
            node = next((n for n in idle if n.free() > 0), None)
            if node is None:
                break
            ok, _ = self._eligible(task, node)
            if ok:
                self._launch(task, node, speculative=True)
                launched = True
        return launched

    def _launch(self, task: Task, node: Node, speculative: bool) -> None:
        if not task.future.set_running_or_notify_cancel() and not task.future.running():
            task.queued = False
            return
        att = Attempt(uuid.uuid4().hex, task, node, time.monotonic(), speculative)
        task.attempts[att.id] = att
        task.queued = False
        self.attempts[att.id] = att
        node.running[att.id] = att
        try:
            sent = node.send_task(att)
            task.future.placement.transfer_bytes += sent
        except ConnectionClosed:
            node.running.pop(att.id, None)
            task.attempts.pop(att.id, None)
            task.queued = True

    def _fail(self, task: Task, exc: BaseException) -> None:
        task.queued = False
        if not task.future.done():
            if not task.future.running():
                task.future.set_running_or_notify_cancel()
            task.future.set_exception(exc)

    # ---------------------------------------------------------- messages

    def _on_message(self, node: Node, msg: dict, bufs: list[bytearray]) -> None:
        t = msg["t"]
        if t == "result":
            self._on_result(node, msg, bufs)
        elif t == "need":
            with self._cv:
                items = [(h, self._buf_by_hash.get(h)) for h in msg["hashes"]]
            if any(b is None for _, b in items):
                return  # task already finished; nothing to send
            node.conn.send({"t": "put", "hashes": [h for h, _ in items]}, [b[0] for _, b in items])
        elif t == "import":
            threading.Thread(target=self._serve_import, args=(node, msg), daemon=True).start()
        elif t == "state":
            with self._cv:
                node.state = {k: v for k, v in msg.items() if k != "t"}
                self._cv.notify_all()
        elif t == "pong":
            node.state = msg.get("state", node.state)
        elif t == "echo":
            node.source.on_echo(node, msg, bufs) if hasattr(node.source, "on_echo") else None

    def _serve_import(self, node: Node, msg: dict) -> None:
        try:
            info, data = envinfo.package_module(msg["name"])
        except Exception as e:
            info, data = {"kind": "error", "detail": repr(e)}, None
        same = data is not None and info.get("hash") == msg.get("have")
        try:
            node.conn.send({"t": "module", "req": msg["req"], "info": {k: v for k, v in info.items() if k != "paths"},
                            "same": same}, [] if same or data is None else [data])
        except ConnectionClosed:
            pass

    def _on_result(self, node: Node, msg: dict, bufs: list[bytearray]) -> None:
        with self._cv:
            att = self.attempts.pop(msg["id"], None)
            for h in msg.get("evicted", ()):
                node.known.discard(h)
            if "state" in msg:
                node.state = msg["state"]
            if att is None:
                self._cv.notify_all()
                return
            att.node.running.pop(att.id, None)
            node.running.pop(att.id, None)
            task = att.task
            self._cv.notify_all()
        elapsed = time.monotonic() - att.started
        timing = msg.get("timing", {})
        if msg.get("stdout"):
            print(msg["stdout"], end="", flush=True)
        if msg.get("stderr"):
            import sys
            print(msg["stderr"], end="", file=sys.stderr, flush=True)
        if task.done:
            return
        kind = node.kind
        if msg.get("ok"):
            try:
                value = serialize.loads(bufs[0], bufs[1:])
            except Exception as e:  # result can't be rebuilt on the Mac
                return self._fail(task, e)
            self.stats.add(task.fn_key, kind, timing.get("run", elapsed))
            self._measure_link(node, att, elapsed, timing, sum(len(b) for b in bufs))
            if task.confirm_error is not None:
                task.reason = (f"{task.confirm_error[0]} raised {task.confirm_error[1]} on the phone; "
                               "the Mac ran it fine, so this function now stays on the Mac")
                self.mac_only[task.fn_key] = (task.reason, None)
            self._finish(task, node, att, value=value)
            return
        if msg.get("unavailable"):
            u = msg["unavailable"]
            reason = _unavailable_reason(u, node)
            suggestion = _unavailable_suggestion(u)
            if u.get("module"):
                node.mac_only_modules.setdefault(u["module"], reason)
                self.missing_packages[u["module"]] += 1
            if task.placement not in ("auto", None) or self.strict:
                return self._fail(task, PlacementError(f"{task.fn_label}: {reason}"
                                                       + (f". {suggestion}" if suggestion else "")))
            self.mac_only[task.fn_key] = (reason, suggestion)
            self._requeue(task, node, reason, suggestion)
            return
        # The task raised. Raise it as-is on the Mac, unless it came from the
        # phone: then confirm on the Mac first so platform differences never
        # surface as errors the Mac wouldn't produce.
        exc = None
        if bufs:
            try:
                exc = serialize.loads(bufs[0], bufs[1:])
            except Exception:
                exc = None
        if exc is None:
            exc = RuntimeError(f"{msg.get('exc_type', 'Error')} on {node.label()}")
        if isinstance(exc, BaseException):
            exc.__cause__ = RemoteTraceback(msg.get("error", ""))
        if node.kind != "mac" and self.confirm_errors and task.placement in ("auto", None):
            task.confirm_error = (node.label(), msg.get("exc_type", "an error"), exc)
            self._requeue(task, node, f"{msg.get('exc_type')} on {node.label()}; re-running on the Mac to confirm",
                          None, quiet=True)
            return
        if task.confirm_error is not None and node.kind == "mac":
            task.reason = "raised on the phone and on the Mac too"
        self._finish(task, node, att, exc=exc)

    def _requeue(self, task: Task, node: Node, reason: str, suggestion: str | None, quiet=False) -> None:
        with self._cv:
            task.excluded[node.kind] = reason
            task.reason = reason
            task.suggestion = suggestion
            if not task.done and not any(a.node.connected and a.id in self.attempts for a in task.attempts.values()):
                task.queued = True
                self.queue.appendleft(task)
            self._cv.notify_all()
        if not quiet:
            self._note(("requeue", task.fn_key, reason),
                       f"pocketnode: {task.fn_label} runs on the Mac: {reason}"
                       + (f"\n  tip: {suggestion}" if suggestion else ""))

    def _finish(self, task: Task, node: Node, att: Attempt, value=None, exc=None) -> None:
        p = task.future.placement
        p.node, p.kind = node.name, node.kind
        p.attempts = [f"{a.node.label()}{' (speculative)' if a.speculative else ''}" for a in task.attempts.values()]
        p.seconds = time.monotonic() - task.submitted
        if att.speculative:
            p.reason = "duplicate of a task stuck on a slower node; finished first"
        elif task.reason:
            p.reason = task.reason
        elif task.placement not in ("auto", None):
            p.reason = f"placement={task.placement!r}"
        else:
            p.reason = "first free slot able to run it"
        p.suggestion = task.suggestion
        self.history.append((task.fn_label, node.name, node.kind, task.reason))
        with self._cv:
            for a in task.attempts.values():
                if a.id != att.id and a.id in self.attempts:
                    self.attempts.pop(a.id, None)
                    a.node.running.pop(a.id, None)
                    try:
                        a.node.conn.send({"t": "cancel", "id": a.id})
                    except ConnectionClosed:
                        pass
        if task.future.done():
            return
        try:
            if exc is not None:
                task.future.set_exception(exc)
            else:
                task.future.set_result(value)
        except Exception:
            pass  # cancelled meanwhile

    def _measure_link(self, node: Node, att: Attempt, elapsed: float, timing: dict, result_bytes: int) -> None:
        if node.kind == "mac":
            return
        busy = sum(timing.get(k, 0) for k in ("prep", "load", "run", "dump"))
        wire = elapsed - busy
        moved = att.task.future.placement.transfer_bytes + result_bytes
        if wire > 0 and moved > 4 << 20:
            node.bandwidth = 0.7 * node.bandwidth + 0.3 * (moved / wire)

    def _note(self, key, text: str) -> None:
        if not self.notes:
            return
        if key is not None:
            if key in self._noted:
                return
            self._noted.add(key)
        log.info(text)


class PlacedExecutor:
    """`ex.on("phone")`: same executor, forced placement."""

    def __init__(self, ex: PocketExecutor, placement: str):
        self._ex, self._p = ex, placement

    def submit(self, fn, /, *args, **kwargs):
        return self._ex._submit(fn, args, kwargs, self._p, None)

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return self._ex.map(fn, *iterables, timeout=timeout, chunksize=chunksize, placement=self._p)


def _matches(node: Node, placement: str) -> bool:
    return placement in (node.kind, node.name, node.id)


def _unavailable_reason(u: dict, node: Node) -> str:
    kind = u.get("kind")
    if kind == "module":
        return f"needs {u.get('module')} (compiled), which isn't installed on {node.name}"
    if kind == "mac-file":
        return u["detail"]
    if kind == "process":
        return u["detail"]
    return f"{kind}: {u.get('detail')}"


def _unavailable_suggestion(u: dict) -> str | None:
    kind = u.get("kind")
    if kind == "module":
        return f"add it to the phone: `pocketnode packages add {u.get('module')}`"
    if kind == "mac-file":
        return "load the file on the Mac and pass the data (e.g. a DataFrame) as an argument; it then travels once and can run on the phone"
    if kind == "process":
        return "use threads (joblib/concurrent.futures threads) inside the task, or let pocketnode spread the work instead"
    return None


def _chunks(it: Iterable, n: int):
    it = iter(it)
    while chunk := list(itertools.islice(it, n)):
        yield chunk


def _run_chunk(fn, chunk):
    return [fn(*args) for args in chunk]


def _result_iter(futs, timeout, flatten=False):
    end = None if timeout is None else time.monotonic() + timeout

    def gen():
        try:
            futs.reverse()
            while futs:
                f = futs.pop()
                r = f.result(None if end is None else end - time.monotonic())
                if flatten:
                    yield from r
                else:
                    yield r
        finally:
            for f in futs:
                f.cancel()

    return gen()


def placement(where: str):
    """`with pn.placement("phone"): ...` forces placement for submits in the block."""
    import contextlib

    @contextlib.contextmanager
    def cm():
        tok = _placement_ctx.set(where)
        try:
            yield
        finally:
            _placement_ctx.reset(tok)

    return cm()


def runs_on(where: str):
    """Decorator: `@pn.runs_on("mac")` pins a function's placement wherever it's submitted."""

    def deco(fn):
        fn.__pocketnode_placement__ = where
        return fn

    return deco
