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
import queue
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
    ro: int | None = None  # buffers this big or bigger are shared read-only
    pure: bool = False  # only stdlib/pure-Python modules: may run in a phone subinterpreter
    weight: int = 1  # calls inside this task (a joblib batch or a map chunk); stats are per call
    shadow: bool = False  # the Mac's copy of a task whose phone result is being verified
    verifying: bool = False  # its phone result is waiting for the Mac's
    cache_retries: int = 0
    crashes: int = 0  # times the node running it went away mid-task
    eligible: dict = field(default_factory=dict)  # node id -> True, once checked
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

    def add(self, key: str, kind: str, secs: float, weight: int = 1) -> None:
        secs /= max(1, weight)
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
        self.verdicts: dict[frozenset, router.Verdict] = {}
        self.packages = {router._norm(k): v for k, v in hello.get("packages", {}).items()}
        self.bandwidth = source.bandwidth_guess  # bytes/s, refined by measurement
        self.latency = source.latency_guess
        self.bytes_sent = 0
        self.lost_as_crash = False
        self.scanned = (-1, 0.0)  # (queue generation, when) of the last scan that found nothing to run
        self.outbox: queue.Queue = queue.Queue()
        threading.Thread(target=self._sender, daemon=True, name=f"pn-send-{self.name}").start()

    def post(self, header: dict, bufs=()) -> None:
        """Queue a message for this node. Only the sender thread writes to the socket,
        so a reader thread never blocks on a full send buffer (which can deadlock)."""
        self.outbox.put((header, list(bufs)))

    def _sender(self) -> None:
        while True:
            item = self.outbox.get()
            if item is None:
                return
            if isinstance(item, tuple):
                try:
                    self.conn.send(*item)
                except (ConnectionClosed, OSError):
                    pass
                continue
            att = item
            if att.task.done or att.id not in self.ex.attempts:
                continue
            try:
                att.task.future.placement.transfer_bytes += self.send_task(att)
            except (ConnectionClosed, OSError):
                self.ex._send_failed(att)

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
        header = {"t": "task", "id": att.id, "bufs": specs, "fk": t.fn_key}
        if t.pure:
            header["pure"] = 1
            header["mods"] = sorted(t.packed.modules - router.IGNORE)
        if t.ro is not None:
            header["ro"] = t.ro
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

def _unwrap(fn, args):
    """Look through batching wrappers: (the function doing the work, calls in this task, its args).

    A joblib batch and a map() chunk are containers. Timing, verification and
    "keep it on the Mac" decisions belong to the function inside, per call.
    """
    if fn is _run_chunk:
        inner, chunk = args
        return inner, max(1, len(chunk)), (chunk[0] if chunk else ())
    items = getattr(fn, "items", None)
    if type(fn).__name__ == "BatchedCalls" and items:
        f, a, _ = items[0]
        return f, len(items), a
    return fn, 1, args


def _fn_identity(fn, args=()) -> tuple[str, str]:
    f = fn
    for _ in range(6):  # functools.partial, bound methods, sklearn's delayed wrapper, decorators
        inner = (getattr(f, "func", None) or getattr(f, "__func__", None) or getattr(f, "function", None)
                 or getattr(f, "__wrapped__", None))
        if inner is None or not callable(inner):
            break
        f = inner
    code = getattr(f, "__code__", None)
    label = getattr(f, "__qualname__", None) or type(f).__name__
    # The same function on different kinds of input is different work
    # (scikit-learn runs every estimator through one _fit_and_score).
    kinds = ",".join(type(a).__name__ for a in tuple(args)[:3])
    if code is not None:
        label = f"{label} ({os.path.basename(code.co_filename)}:{code.co_firstlineno})"
        h = hashlib.sha1(code.co_code + repr(code.co_consts).encode("utf-8", "replace")).hexdigest()[:12]
        return f"{getattr(f, '__module__', '')}.{f.__qualname__}:{h}({kinds})", label
    return f"{type(f).__module__}.{type(f).__qualname__}({kinds})", label


class PocketExecutor(Executor):
    def __init__(self, *, placement: str = "auto", strict: bool = False, version_policy: str = "minor",
                 speculate: bool = True, confirm_errors: bool = True, notes: bool = True,
                 verify: bool = True):
        self.session = uuid.uuid4().hex
        # First phone result per function is checked against the Mac before it's trusted.
        self.verify = verify
        self.verified: dict[str, str] = {}  # fn_key -> "pending" | "ok" | "mac"
        self._verify_wait: dict[str, list] = defaultdict(list)
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
        self.crashes: dict[str, int] = defaultdict(int)
        self._gone: dict[str, Node] = {}  # disconnected nodes, in case the same worker comes back
        self._cv = threading.Condition()
        self._shutdown = False
        self._noted: set[tuple] = set()
        self._buf_by_hash: dict[str, tuple[memoryview, int]] = {}
        self._pins: dict[tuple, tuple] = {}
        self._replies: dict[str, tuple] = {}
        self._qgen = 0  # bumped whenever a task becomes available to pick
        self.history: deque[tuple[str, str, str, str]] = deque(maxlen=5000)
        self._sched = threading.Thread(target=self._loop, daemon=True, name="pn-scheduler")
        self._sched.start()
        serialize.register_user_modules()
        threading.Thread(target=envinfo.manifest, daemon=True).start()  # warm the env scan

    # --------------------------------------------------------------- API

    def add_source(self, src: Source) -> None:
        self.sources.append(src)
        src.start(self)

    def submit(self, fn: Callable, /, *args, **kwargs) -> PocketFuture:
        return self._submit(fn, args, kwargs, None, None)

    def _submit(self, fn, args, kwargs, placement, memo, ro: int | None = None) -> PocketFuture:
        if self._shutdown:
            raise RuntimeError("cannot schedule new futures after shutdown")
        serialize.register_user_modules()
        where = (placement or getattr(fn, "__pocketnode_placement__", None)
                 or _placement_ctx.get() or self.default_placement)
        packed = serialize.dumps((fn, args, kwargs), memo=memo)
        inner, weight, inner_args = _unwrap(fn, args)
        key, label = _fn_identity(inner, inner_args)
        return self._enqueue(packed, key, label, where, ro, weight=weight)

    def _enqueue(self, packed, key, label, where, ro=None, weight=1, shadow=False) -> PocketFuture:
        fut = PocketFuture()
        task = Task(uuid.uuid4().hex, key, label, packed, fut, where, ro=ro, pure=router.is_pure(packed.modules),
                    weight=weight, shadow=shadow)
        with self._cv:
            for b, h in zip(packed.buffers, packed.hashes):
                self._buf_by_hash[h] = (b, self._buf_by_hash.get(h, (b, 0))[1] + 1)
            self.tasks[task.id] = task
            if shadow:  # someone's result is waiting on this: it goes first
                self.queue.appendleft(task)
            else:
                self.queue.append(task)
            self._qgen += 1
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

    @property
    def _max_workers(self) -> int:
        """Concurrent slots right now (dask reads this to size its thread pool)."""
        return max(1, sum(n.effective_slots() for n in self.nodes.values()))

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
        self._pins[tuple(packed.hashes)] = (packed, on)
        for n in targets:
            self._pin_on(n, packed)
        return obj

    def _pin_on(self, n: Node, packed) -> None:
        new = [(h, b) for h, b in zip(packed.hashes, packed.buffers) if h not in n.known]
        if new:
            n.post({"t": "put", "hashes": [h for h, _ in new]}, [b for _, b in new])
            n.known.update(h for h, _ in new)
        n.post({"t": "pin", "hashes": packed.hashes})

    def unpin(self, obj) -> None:
        packed = serialize.dumps(obj, scan=False)
        self._pins.pop(tuple(packed.hashes), None)
        for n in self.nodes.values():
            n.post({"t": "unpin", "hashes": packed.hashes})

    def stacks(self, what: str = "phone", timeout: float = 10.0) -> str:
        """What every thread on a node is doing right now (for a task that seems stuck)."""
        node = next((n for n in self.nodes.values() if _matches(n, what)), None)
        if node is None:
            raise PlacementError(f"no connected node matches {what!r}")
        req, ev = uuid.uuid4().hex, (threading.Event(), [])
        self._replies[req] = ev
        node.post({"t": "stacks", "req": req})
        if not ev[0].wait(timeout):
            self._replies.pop(req, None)
            raise TimeoutError(f"{node.name} didn't answer (its interpreter may be stuck in native code)")
        return ev[1][0]

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
            if task.verifying:
                w = self._verify_wait.get(task.fn_key)
                if w:
                    w[:] = [e for e in w if e[0] is not task]
            for h in task.packed.hashes:
                b, n = self._buf_by_hash.get(h, (None, 0))
                if n <= 1:
                    self._buf_by_hash.pop(h, None)
                else:
                    self._buf_by_hash[h] = (b, n - 1)
            for a in list(task.attempts.values()):
                self.attempts.pop(a.id, None)
                if a.node.running.pop(a.id, None) is not None:
                    a.node.post({"t": "cancel", "id": a.id})
            self._cv.notify_all()

    def _add_node(self, node: Node) -> None:
        with self._cv:
            old = [n for n in self.nodes.values() if n.id == node.id]
            gone = self._gone.pop(node.id, None)
            for o in old + ([gone] if gone else []):
                # The same worker process reconnecting: its cache and in-flight
                # attempts survived, so those tasks are running, not lost.
                node.known = o.known
                node.running = {k: a for k, a in o.running.items() if k in self.attempts}
                for a in node.running.values():
                    a.node = node
                    t = a.task
                    if t.queued and not t.done:
                        t.queued = False
                    if o.lost_as_crash and t.excluded.get(node.kind, "").endswith("stopped while running it"):
                        del t.excluded[node.kind]
                        self.crashes[t.fn_key] = max(0, self.crashes[t.fn_key] - 1)
            self.nodes[node.id] = node
            self._cv.notify_all()
        if node.kind != "mac":
            self._note(("node", node.name), f"pocketnode: {node.label()} connected "
                       f"({node.slots} slots, Python {'.'.join(map(str, node.hello['python']))})")
            crash = node.hello.get("last_crash")
            if crash:
                lines = [l.strip() for l in crash.splitlines() if l.strip()]
                top = next((l for l in lines if l.startswith("Fatal Python error")), lines[0] if lines else "")
                where = next((l for l in lines if l.startswith("File ") and "/pocketnode/" not in l
                              and "/concurrent/" not in l and "/threading.py" not in l), "")
                top = f"{top}; {where}" if where else top
                self._note(("crash", node.id), f"pocketnode: the app on {node.name} crashed last time ({top}); "
                           "details in ex.nodes[...].hello['last_crash']")
            for packed, on in self._pins.values():  # data pinned earlier lives on this node again
                if _matches(node, on):
                    self._pin_on(node, packed)

    def _on_disconnect(self, node: Node) -> None:
        node.outbox.put(None)
        with self._cv:
            node.connected = False
            if self.nodes.get(node.id) is node:
                del self.nodes[node.id]
            # In-flight work there is now suspect: make it runnable elsewhere.
            # A late result (after reconnect) is still accepted if it comes first.
            # If the app went away while active (not backgrounded), the task may
            # have crashed it: run that task on the Mac, and keep a function that
            # does it twice off the phone for good.
            crashed = node.kind != "mac" and node.state.get("lifecycle", "active") == "active"
            node.lost_as_crash = crashed
            if node.kind != "mac":
                self._gone[node.id] = node
            for a in list(node.running.values()):
                t = a.task
                if crashed and t.placement not in ("auto", None) and not t.done:
                    # Told to run there, and it takes the app down: once more in
                    # case it was a coincidence, then say so instead of looping.
                    t.crashes += 1
                    if t.crashes >= 2:
                        self._fail(t, PlacementError(
                            f"{t.fn_label} stopped the app on {node.label()} twice (a crash in native code, "
                            f"or it ran out of memory). After it relaunches, ex.nodes[...].hello['last_crash'] "
                            "has the crash report."))
                        continue
                if crashed and t.placement in ("auto", None) and not t.done:
                    t.excluded[node.kind] = f"the {node.kind} app stopped while running it"
                    self.crashes[t.fn_key] += 1
                    if self.crashes[t.fn_key] >= 2:
                        self.mac_only[t.fn_key] = (f"stopped the {node.kind} app twice", None)
                if not t.done and not t.queued and not any(
                    x.node.connected and x.id != a.id for x in t.attempts.values()
                ):
                    t.queued = True
                    self.queue.appendleft(t)
                    self._qgen += 1
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
        if task.eligible.get(node.id):
            return True, ""
        if node.kind != "mac":
            # The phone holds each input once in its cache and once more as the
            # task's own writable copy; sending something it can't hold would
            # get the app killed instead of answered.
            need = sum(b.nbytes * (1 if task.ro is not None and b.nbytes >= task.ro else 2)
                       for b in task.packed.buffers)
            avail = node.state.get("avail_mem")
            if avail and need > 0.8 * avail:
                why = (f"its inputs need about {need / 2**30:.1f} GB on {node.name}, "
                       f"which has {avail / 2**30:.1f} GB free")
                if p not in ("auto", None) or self.strict:
                    self._fail(task, PlacementError(f"{task.fn_label} can't run on {node.label()}: {why}"))
                else:
                    task.excluded[node.kind] = why
                return False, why
        mods = frozenset(task.packed.modules)
        v = node.verdicts.get(mods)
        if v is None:
            v = node.verdicts[mods] = router.check(task.packed.modules, node, self.version_policy)
        if not v.ok:
            if p not in ("auto", None) or self.strict:
                self._fail(task, PlacementError(f"{task.fn_label} can't run on {node.label()}: {v.reason}"
                                                + (f". {v.suggestion}" if v.suggestion else "")))
            else:
                task.excluded[node.kind] = v.reason
                if v.missing:
                    self._missing(v.missing, task)
                task.suggestion = v.suggestion
            return False, v.reason
        task.eligible[node.id] = True
        return True, ""

    def _estimate(self, task: Task, node: Node) -> float | None:
        c = self.stats.get(task.fn_key, node.kind)
        if c is None:
            return None
        tc, _ = node.transfer_cost(task)
        return c * task.weight + tc

    def _mac_finish_estimate(self, task: Task, queued: int | None = None) -> float | None:
        macs = [n for n in self.nodes.values() if n.kind == "mac"]
        c = self.stats.get(task.fn_key, "mac")
        if not macs or c is None:
            return None
        slots = sum(n.effective_slots() for n in macs) or 1
        if queued is None:
            queued = sum(1 for t in self.queue if t.queued)
        return (queued / slots + 1) * c * task.weight

    def _pick(self, node: Node) -> Task | None:
        """Next queued task this node should run, or None."""
        queued = None
        for task in self.queue:  # nothing in this loop adds to or removes from the queue
            if task.done or not task.queued:
                continue
            ok, _ = self._eligible(task, node)
            if task.done:
                continue
            if not ok:
                continue
            if node.kind != "mac" and task.placement in ("auto", None):
                mine = self._estimate(task, node)
                if mine is not None and queued is None:
                    queued = sum(1 for t in self.queue if t.queued)
                mac = self._mac_finish_estimate(task, queued) if mine is not None else None
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
        now = time.monotonic()
        for node in nodes:
            # A node that just went through the whole queue and found nothing it
            # should run doesn't look again until something new is queued (or a
            # moment passes: estimates drift as results come in).
            if node.scanned[0] == self._qgen and now - node.scanned[1] < 0.5:
                continue
            while node.free() > 0:
                task = self._pick(node)
                if task is None:
                    node.scanned = (self._qgen, now)
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
            if task.done or task.queued or task.verifying or len(task.attempts) >= 2:
                continue
            atts = list(task.attempts.values())
            if not atts or any(a.node.kind == "mac" for a in atts):
                continue
            a = atts[0]
            mac_t = self.stats.get(task.fn_key, "mac")
            mac_t = mac_t * task.weight if mac_t is not None else None
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
        if not _mark_running(task.future):
            task.queued = False
            return
        att = Attempt(uuid.uuid4().hex, task, node, time.monotonic(), speculative)
        task.attempts[att.id] = att
        task.queued = False
        self.attempts[att.id] = att
        node.running[att.id] = att
        node.outbox.put(att)  # sent by the node's own thread, outside the scheduler lock

    def _send_failed(self, att: Attempt) -> None:
        with self._cv:
            att.node.running.pop(att.id, None)
            att.task.attempts.pop(att.id, None)
            self.attempts.pop(att.id, None)
            t = att.task
            if not t.done and not t.queued and not any(a.id in self.attempts for a in t.attempts.values()):
                t.queued = True
                self.queue.appendleft(t)
                self._qgen += 1
            self._cv.notify_all()

    def _fail(self, task: Task, exc: BaseException) -> None:
        task.queued = False
        if _mark_running(task.future):
            try:
                task.future.set_exception(exc)
            except Exception:
                pass

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
            node.post({"t": "put", "hashes": [h for h, _ in items]}, [b[0] for _, b in items])
        elif t == "import":
            threading.Thread(target=self._serve_import, args=(node, msg), daemon=True).start()
        elif t == "state":
            with self._cv:
                node.state = {k: v for k, v in msg.items() if k != "t"}
                self._cv.notify_all()
        elif t == "pong":
            node.state = msg.get("state", node.state)
        elif t == "stacks":
            ev = self._replies.pop(msg.get("req"), None)
            if ev:
                ev[1].append(msg.get("text", ""))
                ev[0].set()
        elif t == "echo":
            node.source.on_echo(node, msg, bufs) if hasattr(node.source, "on_echo") else None

    def _serve_import(self, node: Node, msg: dict) -> None:
        try:
            info, data = envinfo.package_module(msg["name"])
        except Exception as e:
            info, data = {"kind": "error", "detail": repr(e)}, None
        same = data is not None and info.get("hash") == msg.get("have")
        node.post({"t": "module", "req": msg["req"], "info": {k: v for k, v in info.items() if k != "paths"},
                   "same": same}, [] if same or data is None else [data])

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
            self.stats.add(task.fn_key, kind, timing.get("run", elapsed), task.weight)
            self._measure_link(node, att, elapsed, timing, sum(len(b) for b in bufs))
            if task.confirm_error is not None:
                task.reason = (f"{task.confirm_error[0]} raised {task.confirm_error[1]} on the phone; "
                               "the Mac ran it fine, so this function now stays on the Mac")
                self.mac_only[task.fn_key] = (task.reason, None)
            # Verify automatically placed work; forced placement means "run it there".
            if (node.kind != "mac" and self.verify and task.placement in ("auto", None)
                    and not task.shadow and self.verified.get(task.fn_key) != "ok"):
                self._verify(task, node, att, value)
                return
            self._finish(task, node, att, value=value)
            return
        if msg.get("unavailable"):
            u = msg["unavailable"]
            if u.get("kind") == "cache" and task.cache_retries < 2:
                # Its inputs were evicted between arriving and running: send them again.
                task.cache_retries += 1
                with self._cv:
                    node.known.difference_update(task.packed.hashes)
                    if not task.done and not any(a.id in self.attempts for a in task.attempts.values()):
                        task.queued = True
                        self.queue.appendleft(task)
                        self._qgen += 1
                    self._cv.notify_all()
                return
            reason = _unavailable_reason(u, node)
            suggestion = _unavailable_suggestion(u)
            if u.get("module"):
                node.mac_only_modules.setdefault(u["module"], reason)
                node.verdicts.clear()
                self._missing(u["module"], task)
            if task.placement not in ("auto", None) or self.strict:
                return self._fail(task, PlacementError(f"{task.fn_label}: {reason}"
                                                       + (f". {suggestion}" if suggestion else "")))
            if u.get("kind") not in ("memory", "cache"):  # those depend on the inputs, not the function
                self.mac_only[task.fn_key] = (reason, suggestion)
            self._requeue(task, node, reason, suggestion, quiet=bool(u.get("module")))
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

    # ----------------------------------------------------- verification

    def _verify(self, task: Task, node: Node, att: Attempt, value) -> None:
        """Hold a phone result until the Mac has computed the same task and they agree."""
        key = task.fn_key
        with self._cv:
            state = self.verified.get(key)
            if state == "mac":  # decided meanwhile: this function runs on the Mac
                self._requeue(task, node, self.mac_only.get(key, ("",))[0], None, quiet=True)
                return
            if state is None and not any(n.kind == "mac" for n in self.nodes.values()):
                state = self.verified[key] = "ok"  # nothing to check against
            if state != "ok":
                task.verifying = True
                self._verify_wait[key].append((task, node, att, value))
                if state is None:
                    self.verified[key] = "pending"
        if state == "ok":
            self._finish(task, node, att, value=value)
        elif state is None:
            self._shadow(key, task, [])

    def _shadow(self, key: str, task: Task, earlier: list) -> None:
        """Run `task` on the Mac ahead of the queue; its phone result waits for the answer."""
        fut = self._enqueue(task.packed, key, task.fn_label, "mac", task.ro, weight=task.weight, shadow=True)
        fut.add_done_callback(lambda f: self._verify_done(key, task, [*earlier, f]))

    def _verify_done(self, key: str, task0: Task, macs: list) -> None:
        from . import verify as V
        with self._cv:
            entry = next((w for w in self._verify_wait.get(key, ()) if w[0] is task0), None)
        if entry is None:  # the task was cancelled or finished another way
            with self._cv:
                self.verified.pop(key, None) if self.verified.get(key) == "pending" else None
                pending = self._verify_wait.pop(key, [])
            for t, n, a, v in pending:
                self._verify(t, n, a, v)
            return
        phone_value = entry[3]
        first = macs[0]
        exc = first.exception()
        note = None
        if exc is not None:
            agree, why = False, f"the Mac raised {type(exc).__name__}"
        else:
            diffs = V.diff(phone_value, first.result())
            agree, why = not diffs, (f"{diffs[0][0] or 'result'}: {diffs[0][1]}" if diffs else "")
            if diffs and len(macs) == 1:
                # Before blaming the phone: does the Mac agree with itself?
                self._shadow(key, task0, macs)
                return
            if diffs:
                second = macs[1]
                unstable = [("", "raised")] if second.exception() is not None else V.diff(first.result(), second.result())
                real = V.outside(diffs, unstable)
                agree = not real
                if real:
                    why = f"{real[0][0] or 'result'}: {real[0][1]}"
                elif any(p == "" for p, _ in unstable):
                    note = (f"pocketnode: {task0.fn_label} gives a different result on every run, so its "
                            "phone results can't be checked against the Mac; trusting them")
                # Otherwise only some fields vary run to run (timings, say) and the rest matched.
        with self._cv:
            pending = self._verify_wait.pop(key, [])
            self.verified[key] = "ok" if agree else "mac"
            for t, *_ in pending:
                t.verifying = False
        if note:
            self._note(("nondet", key), note)
        if agree:
            for t, n, a, v in pending:
                self._finish(t, n, a, value=v)
            return
        reason = f"its phone result differed from the Mac's ({why}), so it now runs on the Mac"
        self.mac_only[key] = (reason, None)
        self._note(("verify", key), f"pocketnode: {task0.fn_label}: {reason}")
        for t, n, a, _ in pending:
            if t is task0:
                t.reason = reason
                if exc is not None:
                    self._finish(t, n, a, exc=exc)
                else:
                    self._finish(t, n, a, value=first.result())
            else:
                self._requeue(t, n, reason, None, quiet=True)

    def _requeue(self, task: Task, node: Node, reason: str, suggestion: str | None, quiet=False) -> None:
        with self._cv:
            task.excluded[node.kind] = reason
            task.reason = reason
            task.suggestion = suggestion
            if not task.done and not any(a.node.connected and a.id in self.attempts for a in task.attempts.values()):
                task.queued = True
                self.queue.appendleft(task)
                self._qgen += 1
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
                    a.node.post({"t": "cancel", "id": a.id})
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

    def _missing(self, package: str, task: Task) -> None:
        """A compiled package kept a task off the phone: offer to add it, once, with an estimate."""
        first = package not in self.missing_packages
        self.missing_packages[package] += 1
        if not first or not self.notes:
            return

        def offer():
            try:
                from .provision import app, packages
                packages.record_miss(package)
                if package in app.config().get("always_mac", []):
                    return
                est = packages.estimate([package])
                how = "measured" if est["measured"] else "first-time estimate"
                total = est["build_s"] + est["install_s"]
                when = f"~{total // 60} min" if total >= 90 else f"~{total} s"
                self._note(("missing", package),
                           f"pocketnode: {package} isn't on the phone, so {task.fn_label} ran on the Mac.\n"
                           f"  add it ({when}: build ~{est['build_s']} s, install/restart ~{est['install_s']} s, "
                           f"{how}): pn.packages.add({package!r})\n"
                           f"  or keep it on the Mac without asking again: pn.packages.always_mac({package!r})")
            except Exception:  # noqa: BLE001 - the offer is advisory
                log.debug("pocketnode: couldn't estimate adding %s", package, exc_info=True)

        threading.Thread(target=offer, daemon=True).start()

    def _note(self, key, text: str) -> None:
        if not self.notes:
            return
        if key is not None:
            if key in self._noted:
                return
            self._noted.add(key)
        log.info(text)


def _mark_running(fut: Future) -> bool:
    """PENDING -> RUNNING; True if the future is (now) running, False if cancelled or done."""
    if fut.running():
        return True
    if fut.done():
        return False
    try:
        return fut.set_running_or_notify_cancel()
    except RuntimeError:
        return fut.running()


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
    if kind in ("process", "memory"):
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
    if kind == "memory":
        return "pass smaller pieces (e.g. df.pn.map_partitions or ex.map over chunks) and they fit on the phone"
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
