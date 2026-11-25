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
import inspect
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

from . import envinfo, pairing, router, serialize, wire
from .errors import NodeLost, PlacementError, RemoteTraceback
from .wire import Connection, ConnectionClosed, client_handshake

log = logging.getLogger("pocketnode")

_placement_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("pocketnode_placement", default=None)

THERMAL_SCALE = {"nominal": 1.0, "fair": 1.0, "serious": 0.5, "critical": 0.0}
# With the app in the background under iOS continued processing, the phone keeps
# computing at roughly 1/15 of its foreground speed (measured: 108 vs 1,649 GFLOPS).
BACKGROUND_SLOWDOWN = 15.0
# A task forced onto a phone that asks before running new code waits this long for Allow.
APPROVAL_WAIT = 120.0
# A forced task fails this long after its phone has turned this Mac away (paused, not paired),
# so a phone that is only reconnecting isn't failed.
BLOCKED_GRACE = 5.0
SRC_LIMIT = 32 << 10  # bytes of source (or disassembly) sent to a store phone per function
# A streaming task sends at most this many items before the Mac has taken them; the Mac says
# what it has taken every STREAM_ACK items.
STREAM_WINDOW = 32
STREAM_ACK = 8


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
    fn: Any = None  # the function doing the work (for its source text, shown on store phones)
    # An object kept on a node (ex.create): "create" builds it there, "call" runs a method on it.
    kind: str = "task"
    obj: Any = None  # the RemoteObject handle
    method: str | None = None
    stream: Any = None  # a RemoteStream: the callable yields, and each item is sent as it comes
    # Runs only where it was placed: if that node goes away it fails with NodeLost instead of
    # running again elsewhere (it may have changed state there, or already sent part of its output).
    sticky: bool = False
    fallback: str | None = None  # placed automatically on a phone that then couldn't: go here instead
    node: Any = None  # the node it finished on
    gen: int = 0  # (calls) the object's loss count when it was made

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
        # Buffers the node holds. Seeded with what is on its disk (sent after the handshake),
        # so inputs that survived an app restart aren't sent again; a "need" covers any miss.
        self.known: set[str] = set(hello.get("cached") or ())
        self.kept: set[str] = set()  # keys of pinned objects sent to this node (ex.pin)
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
        self.src_sent: set[str] = set()  # code hashes whose text this node has been sent (store edition)
        self.held: dict[str, str] = {}  # code hash -> "waiting" | "denied" (store phone reviewing new code)
        self.bye: str | None = None  # why the phone said it was closing the connection
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
                    self.bytes_sent += sum(len(b) for b in item[1] if isinstance(b, (bytes, bytearray)))
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
        sent = 0
        for key, p in t.packed.pins.items():  # pinned objects it uses that this node hasn't got yet
            if key not in self.kept:
                for header, bufs in self.ex._keep_frames(self, p):
                    self.conn.send(header, bufs)
                    sent += sum(wire.as_bytes_view(b).nbytes for b in bufs)
        specs, inline = [], []
        for b, h in zip(t.packed.buffers, t.packed.hashes):
            if h in self.known:
                specs.append({"h": h})
            else:
                specs.append({"h": h, "i": 1})
                inline.append(b)
                self.known.add(h)
        header = {"t": t.kind, "id": att.id, "bufs": specs, "fk": t.fn_key, "label": t.fn_label}
        if t.obj is not None:
            header["obj"] = t.obj._id
        if t.method:
            header["method"] = t.method
        if t.stream is not None:
            header["stream"], header["window"] = 1, STREAM_WINDOW
        if self.hello.get("edition") == "store":
            # The App Store edition shows the person what runs; the text travels once per code.
            ci = self.ex._code_of(t)
            header["code"], header["origin"] = ci["code"], ci["origin"]
            if t.kind == "call":  # allowing the class (or factory) allows its methods
                header["parent"] = self.ex._code_for(t.obj._key, t.obj._factory, t.obj._label)["code"]
            for k in ("dist", "where"):
                if ci.get(k):
                    header[k] = ci[k]
            if ci["code"] not in self.src_sent:
                self.src_sent.add(ci["code"])
                header["src"], header["srckind"] = ci["src"], ci["srckind"]
        if t.pure:
            header["pure"] = 1
            header["mods"] = sorted(t.packed.modules - router.IGNORE)
        if t.ro is not None:
            header["ro"] = t.ro
        if t.packed.pins:
            header["pins"] = list(t.packed.pins)
        self.conn.send(header, [t.packed.data, *inline])
        n = sent + len(t.packed.data) + sum(b.nbytes for b in inline)
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
    # Set while the phone answers but turns this Mac away for a reason only a person can clear
    # (paused there, not paired, pairing forgotten): the sentence saying so, and since when.
    blocked: str | None = None
    blocked_since: float = 0.0

    def block(self, why: str) -> None:
        if self.blocked is None:
            self.blocked_since = time.monotonic()
        self.blocked = why

    def unblock(self) -> None:
        self.blocked = None

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
    }, credentials=getattr(source, "credentials", None) or pairing.credentials_for)
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
    def __init__(self, *, placement: str = "auto", strict: bool = False, version_policy: str | None = None,
                 speculate: bool = True, confirm_errors: bool = True, notes: bool = True,
                 verify: bool = True, auto_add: bool = True):
        self.session = uuid.uuid4().hex
        self.auto_add = auto_add
        self._adding: set[str] = set()  # distributions already offered or added
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
        self._mac_only_mod: dict[str, str] = {}  # fn_key -> the missing module that put it in mac_only
        self.missing_packages: dict[str, int] = defaultdict(int)
        self.crashes: dict[str, int] = defaultdict(int)
        self._gone: dict[str, Node] = {}  # disconnected nodes, in case the same worker comes back
        self._cv = threading.Condition()
        self._shutdown = False
        self._noted: set[tuple] = set()
        self._buf_by_hash: dict[str, tuple[memoryview, int]] = {}
        self._kept: dict[int, _Pin] = {}  # id(obj) -> its pin, for objects kept on nodes by pin()
        self._objects: dict[str, Any] = {}  # object id -> RemoteObject (ex.create), until closed
        self._replies: dict[str, tuple] = {}
        self._qgen = 0  # bumped whenever a task becomes available to pick
        self.history: deque[tuple[str, str, str, str]] = deque(maxlen=5000)
        self._src_cache: dict[str, dict] = {}  # fn_key -> what a store phone is shown (_code_info)
        self._held: dict[str, tuple] = {}  # task id -> (task, node id, code, deadline): waiting for approval
        self._later: list[tuple[Task, bool]] = []  # see _enqueue_task
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
        packed = serialize.dumps((fn, args, kwargs), memo=memo, pins=self._pin_of if self._kept else None)
        inner, weight, inner_args = _unwrap(fn, args)
        key, label = _fn_identity(inner, inner_args)
        return self._enqueue(packed, key, label, where, ro, weight=weight, fn=inner)

    def _enqueue(self, packed, key, label, where, ro=None, weight=1, shadow=False, fn=None) -> PocketFuture:
        fut = PocketFuture()
        # (A task that uses a pinned object runs where that object is kept: the main interpreter.)
        task = Task(uuid.uuid4().hex, key, label, packed, fut, where, ro=ro,
                    pure=router.is_pure(packed.modules) and not packed.pins,
                    weight=weight, shadow=shadow, fn=fn)
        self._admit(task)
        return fut

    def _admit(self, task: Task) -> None:
        """Put a task in the queue (once per task) and free what it holds when it is done."""
        with self._cv:
            for b, h in zip(task.packed.buffers, task.packed.hashes):
                self._buf_by_hash[h] = (b, self._buf_by_hash.get(h, (b, 0))[1] + 1)
            self.tasks[task.id] = task
            self._enqueue_task(task, left=task.shadow)  # a shadow's phone result waits on it: first
            self._cv.notify_all()
        task.future.add_done_callback(lambda f, t=task: self._release(t))

    def _enqueue_task(self, task: Task, left: bool) -> None:
        """Add to the queue (under self._cv). The scheduler can get here from inside its own walk
        over the queue (a task it fails lets an object's next call in): then it waits for the
        next round, since the queue can't change while that walk is going through it."""
        if threading.current_thread() is self._sched:
            self._later.append((task, left))
        elif left:
            self.queue.appendleft(task)
        else:
            self.queue.append(task)
        self._qgen += 1

    def _requeue_task(self, task: Task) -> None:
        """Queue an admitted task that isn't running anywhere again (where task.placement says)."""
        with self._cv:
            if not task.done and not task.queued and not any(a.id in self.attempts for a in task.attempts.values()):
                task.queued = True
                task.eligible.clear()
                self._enqueue_task(task, left=True)
            self._cv.notify_all()

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

    def create(self, factory: Callable, /, *args, recreate: bool = True, **kwargs) -> "RemoteObject":
        """Build `factory(*args, **kwargs)` on one node and keep it there; returns a handle.

        `handle.method(...)` runs that method on the kept object and returns a future. Calls
        on one object run one at a time, in the order made; different objects run at once.
        The node is the first phone able to run it (its modules there, its inputs fitting in
        memory), else a Mac worker; `ex.on("phone").create(...)` insists. Blocks until the
        object is built and raises what the constructor raised.

        Calls run only where the object is, with no checking against the Mac. If that node
        goes away, calls in flight raise NodeLost; the next call rebuilds the object from the
        same arguments once a node of that kind is back (its state starts over), unless
        recreate=False, which makes later calls raise NodeLost instead.
        """
        return self._create(factory, args, kwargs, None, recreate)

    def _create(self, factory, args, kwargs, placement, recreate) -> "RemoteObject":
        if self._shutdown:
            raise RuntimeError("cannot create objects after shutdown")
        from .remote import RemoteObject
        serialize.register_user_modules()
        obj = RemoteObject(self, factory, args, kwargs, placement or self.default_placement, recreate)
        obj._start()
        return obj

    def stream(self, fn: Callable, /, *args, **kwargs) -> "RemoteStream":
        """Run a generator function on one node and iterate over what it yields, as it yields.

        Each item is sent to the Mac when it is produced (the node stays at most a few dozen
        items ahead of you). The iterator ends when the generator does and raises what it
        raises. Leaving the loop early (break, close(), or dropping the iterator) stops the
        generator there. Placement is decided once, when you call this; the work is not checked
        against the Mac or moved, and if the node goes away the iterator raises NodeLost.
        """
        return self._stream(fn, args, kwargs, None)

    def _stream(self, fn, args, kwargs, placement) -> "RemoteStream":
        if self._shutdown:
            raise RuntimeError("cannot schedule new futures after shutdown")
        from .remote import _streaming
        serialize.register_user_modules()
        where = (placement or getattr(fn, "__pocketnode_placement__", None)
                 or _placement_ctx.get() or self.default_placement)
        packed = serialize.dumps((fn, args, kwargs), pins=self._pin_of if self._kept else None)
        key, label = _fn_identity(fn, args)
        task = Task(uuid.uuid4().hex, key, label, packed, PocketFuture(), where, fn=fn, sticky=True)
        st = _streaming(self, task)
        if where in ("auto", None):
            self._place_once(task)
        self._admit(task)
        return st

    def _place_once(self, task: Task) -> None:
        """Automatic placement decided at submission, for work that stays where it starts:
        the first phone that can run it, else a Mac worker."""
        with self._cv:
            task.placement = "auto"
            phones = sorted((n for n in self.nodes.values() if n.kind != "mac" and n.connected),
                            key=lambda n: n.name)
            for n in phones:
                ok, _ = self._eligible(task, n)
                if task.done:
                    return
                if ok:
                    task.placement, task.fallback = n.id, "mac"
                    return
            task.placement = "mac"

    def on(self, placement: str) -> "PlacedExecutor":
        """A view of this executor that forces placement: `ex.on("phone").submit(f, x)`."""
        return PlacedExecutor(self, placement)

    def pin(self, obj, on: str = "phone"):
        """Keep obj loaded on the nodes matching `on`; tasks that use obj then use that copy.

        Every later task that refers to this same object (as an argument, inside a container,
        or through a closure or global) gets the copy kept on the node it runs on instead of
        rebuilding it from bytes. Matching nodes get it now; any other node (a Mac worker,
        say) gets it the first time a task using it runs there. Each node builds it once.

        A pinned object is a shared, read-mostly snapshot: tasks on a node all see the same
        copy, as threads on the Mac would, so tasks shouldn't change it (its large arrays are
        read-only there). Changing it on the Mac doesn't change the kept copies until you call
        ex.pin(obj) again, which sends only what changed. PyTorch tensors and modules are
        checked for in-place changes (their version counters) whenever a task using them is
        sent, and re-pinned by themselves. ex.unpin(obj) frees the copies; shutdown does too.

        Returns obj unchanged, so you keep using it as usual.
        """
        targets = [n for n in self.nodes.values() if _matches(n, on)]
        if not targets:
            raise PlacementError(f"no connected node matches {on!r}")
        self._keep(obj, on, eager=True)
        return obj

    def _keep(self, obj, on: str, eager: bool) -> "_Pin":
        p = _Pin(obj, serialize.dumps(obj), on)
        with self._cv:
            old = self._kept.get(id(obj))
            self._kept[id(obj)] = p
            targets = [n for n in self.nodes.values() if _matches(n, on)] if eager else []
        for n in targets:
            if not _lacks(n, "pinned-objects"):
                for header, bufs in self._keep_frames(n, p):
                    n.post(header, bufs)
        if old is not None and old.obj is obj:
            self._retire(old)
        return p

    def _pin_of(self, obj) -> "_Pin | None":
        """The pin record of obj if obj itself is pinned (called for every object a task pickles)."""
        p = self._kept.get(id(obj))
        if p is None or p.obj is not obj:
            return None
        if p.sig is not None and _versions(obj) != p.sig:  # changed in place since it was pinned
            p = self._keep(obj, p.on, eager=False)
        return p

    def _keep_frames(self, n: Node, p: "_Pin") -> list:
        """What a node needs to hold p: the buffers it lacks, then the object's pickle."""
        new = [(h, b) for h, b in zip(p.packed.hashes, p.packed.buffers) if h not in n.known]
        frames = []
        if new:
            frames.append(({"t": "put", "hashes": [h for h, _ in new]}, [b for _, b in new]))
            n.known.update(h for h, _ in new)
        frames.append(({"t": "keep", "key": p.key, "hashes": p.packed.hashes, "label": p.label},
                       [p.packed.data]))
        n.kept.add(p.key)
        return frames

    def _rekeep(self, n: Node, key: str, missing: list) -> None:
        """A node couldn't find some of a kept object's buffers (its file was gone): send them again."""
        with self._cv:
            n.known.difference_update(missing)
            p = next((p for p in self._kept.values() if p.key == key), None)
        if p is not None and any(wire.digest(b) != h for h, b in zip(p.packed.hashes, p.packed.buffers)
                                 if h in missing):
            return self._refresh(p)  # (hashed outside the lock: a large array takes a moment)
        with self._cv:
            if p is None or p.retries.get(n.id, 0) >= 2:
                n.kept.discard(key)  # a task that needs it asks again ("pin" unavailable)
                return
            p.retries[n.id] = p.retries.get(n.id, 0) + 1
        for header, bufs in self._keep_frames(n, p):
            n.post(header, bufs)

    def _refresh(self, p: "_Pin") -> None:
        """A pinned object's arrays were changed in place on the Mac, so the bytes no longer
        match what was pinned: every node gets the object as it is now, under the same key
        (tasks already made refer to it by that key)."""
        packed = serialize.dumps(p.obj)
        with self._cv:
            p.packed, p.modules, p.sig = packed, set(packed.modules), _versions(p.obj)
            p.nbytes = sum(b.nbytes for b in packed.buffers)
            p.retries.clear()
            nodes = [m for m in self.nodes.values() if p.key in m.kept or _matches(m, p.on)]
            for m in nodes:
                m.kept.discard(p.key)
        for m in nodes:
            if not _lacks(m, "pinned-objects"):
                for header, bufs in self._keep_frames(m, p):
                    m.post(header, bufs)
        self._note(("pin-changed", p.key), f"pocketnode: a pinned {p.label} was changed after ex.pin(); the copies "
                   "kept on the nodes were replaced with it as it is now. Call ex.pin(obj) again after changing it.")

    def _retire(self, p: "_Pin") -> None:
        p.closed = True
        with self._cv:
            if self._kept.get(id(p.obj)) is p:
                del self._kept[id(p.obj)]
            busy = any(p.key in t.packed.pins for t in self.tasks.values())
            nodes = [n for n in self.nodes.values() if p.key in n.kept] if not busy else []
        for n in nodes:  # (if a task still uses it, the copy goes when that task finishes)
            n.kept.discard(p.key)
            n.post({"t": "unkeep", "key": p.key})

    def unpin(self, obj) -> None:
        """Free the copies of obj kept by ex.pin(obj) on every node."""
        p = self._kept.get(id(obj))
        if p is not None and p.obj is obj:
            self._retire(p)

    def stacks(self, what: str = "phone", timeout: float = 10.0) -> str:
        """What every thread on a node is doing right now (for a task that seems stuck)."""
        node = next((n for n in self.nodes.values() if _matches(n, what)), None)
        if node is None:
            raise PlacementError(f"no connected node matches {what!r}")
        req = uuid.uuid4().hex
        reply = self.expect(req)
        node.post({"t": "stacks", "req": req})
        msg = reply(timeout)
        if msg is None:
            raise TimeoutError(f"{node.name} didn't answer (its interpreter may be stuck in native code)")
        return msg.get("text", "")

    def expect(self, req: str) -> Callable[[float | None], dict | None]:
        """Register for a node's reply to request `req`; call the result to wait for it (None on timeout)."""
        ev = self._replies[req] = (threading.Event(), [])

        def wait(timeout: float | None) -> dict | None:
            got = ev[0].wait(timeout)
            self._replies.pop(req, None)
            return ev[1][0] if got and ev[1] else None

        return wait

    def packages_changed(self, node: Node, packages: dict, added: dict, failed: dict | None = None) -> None:
        """A node's packages changed while it runs: route by what it has now."""
        added_n = {router._norm(d) for d in added}
        mods = {m for m, i in envinfo.manifest().items() if router._norm(i["dist"]) in added_n}
        with self._cv:
            node.hello["packages"] = packages
            node.packages = {router._norm(k): v for k, v in packages.items()}
            node.verdicts.clear()
            for m in mods:
                node.mac_only_modules.pop(m, None)
            for key, m in list(self._mac_only_mod.items()):
                if m in mods:
                    self.mac_only.pop(key, None)
                    del self._mac_only_mod[key]
            for k in list(self.missing_packages):
                if k in mods or router._norm(k) in added_n:
                    del self.missing_packages[k]
            for name, text in (failed or {}).items():
                node.mac_only_modules[name] = f"{name} is on {node.name} but failed to import there ({text})"
            self._qgen += 1
            self._cv.notify_all()

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
        self._kept.clear()
        for o in list(self._objects.values()):  # objects kept on nodes go with the executor too
            o._closed = True
            n = o._node
            if n is not None and self.nodes.get(n.id) is n:
                try:
                    n.conn.send({"t": "drop", "obj": o._id})
                except (ConnectionClosed, OSError):
                    pass
        self._objects.clear()
        for n in list(self.nodes.values()):
            for key in list(n.kept):  # pinned objects go with the executor
                try:
                    n.conn.send({"t": "unkeep", "key": key})
                except (ConnectionClosed, OSError):
                    break
            n.kept.clear()
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
            for p in task.packed.pins.values():  # unpinned while this task still used it
                if p.closed and not any(p.key in t.packed.pins for t in self.tasks.values()):
                    for n in self.nodes.values():
                        if p.key in n.kept:
                            n.kept.discard(p.key)
                            n.post({"t": "unkeep", "key": p.key})
            self._cv.notify_all()

    def _add_node(self, node: Node) -> None:
        with self._cv:
            old = [n for n in self.nodes.values() if n.id == node.id]
            gone = self._gone.pop(node.id, None)
            for o in old + ([gone] if gone else []):
                # The same worker process reconnecting: its cache and in-flight
                # attempts survived, so those tasks are running, not lost.
                o.known.update(node.known)
                node.known = o.known
                node.kept = o.kept
                node.held = o.held
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
            theirs = node.hello.get("source")
            if theirs and theirs != envinfo.source_version() and not node.same_env:
                self._note(("stale", node.id), f"pocketnode: the app on {node.name} was built from a different "
                           "version of pocketnode than this one; run `pocketnode app install` to update it")
            crash = node.hello.get("last_crash")
            if crash:
                lines = [l.strip() for l in crash.splitlines() if l.strip()]
                top = next((l for l in lines if l.startswith("Fatal Python error")), lines[0] if lines else "")
                where = next((l for l in lines if l.startswith("File ") and "/pocketnode/" not in l
                              and "/concurrent/" not in l and "/threading.py" not in l), "")
                top = f"{top}; {where}" if where else top
                self._note(("crash", node.id), f"pocketnode: the app on {node.name} crashed last time ({top}); "
                           "details in ex.nodes[...].hello['last_crash']")
            for p in list(self._kept.values()):  # objects pinned earlier are kept on this node again
                if _matches(node, p.on) and p.key not in node.kept and not _lacks(node, "pinned-objects"):
                    for header, bufs in self._keep_frames(node, p):
                        node.post(header, bufs)

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
            # A phone that said goodbye first (paused, or this Mac was forgotten) didn't crash.
            crashed = node.kind != "mac" and node.state.get("lifecycle", "active") == "active" \
                and not node.bye
            node.lost_as_crash = crashed
            if node.kind != "mac":
                self._gone[node.id] = node
            for a in list(node.running.values()):
                t = a.task
                if t.sticky and not t.done:
                    if t.fallback and (t.kind == "create" or (t.stream is not None and not t.stream.started)):
                        # Placed there automatically and nothing came of it yet: start it elsewhere.
                        node.running.pop(a.id, None)
                        self.attempts.pop(a.id, None)
                        t.attempts.pop(a.id, None)
                        self._retarget(t, f"lost {node.label()} while building it")
                    else:
                        self._fail(t, _lost_error(t, node))
                    continue
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
            for t in list(self.queue):  # waiting to run on this node, which is gone
                if t.sticky and t.queued and not t.done and t.placement == node.id:
                    self._fail(t, _lost_error(t, node))
            for o in list(self._objects.values()):
                o._lost(node)
            self._cv.notify_all()
        if node.kind != "mac" and not self._shutdown:
            self._note(None, f"pocketnode: lost {node.label()}; its tasks are being re-run on other nodes")

    def _eligible(self, task: Task, node: Node) -> tuple[bool, str]:
        p = task.placement
        if p not in ("auto", None) and not _matches(node, p):
            return False, ""
        if node.held:
            code = self._code_of(task)["code"]
            state = node.held.get(code)
            if state is not None:
                if p not in ("auto", None):
                    if state == "denied":
                        self._fail(task, PlacementError(
                            f"{task.fn_label} was declined on {node.name} (Don't Allow), so it can't run there"))
                    else:  # held until the person answers (see _check_held)
                        self._held.setdefault(task.id, (task, node.id, code, time.monotonic() + APPROVAL_WAIT))
                    return False, ""
                verb = "is waiting for approval" if state == "waiting" else "was declined"
                return False, f"{task.fn_label} {verb} on {node.name}"
        if node.kind in task.excluded or node.name in task.excluded:
            return False, task.excluded.get(node.kind) or task.excluded[node.name]
        if node.kind != "mac" and task.fn_key in self.mac_only and p in ("auto", None):
            return False, self.mac_only[task.fn_key][0]
        if task.eligible.get(node.id):
            return True, ""
        feature = _feature_for(task)
        if feature and _lacks(node, feature):
            why = OLDER_APP.format(node.name)
            if p not in ("auto", None) or self.strict:
                self._fail(task, PlacementError(f"{task.fn_label} can't run on {node.label()}: {why}"))
            else:
                task.excluded[node.kind] = why
            return False, why
        if node.kind != "mac":
            # The phone holds each input once in its cache and once more as the
            # task's own writable copy; sending something it can't hold would
            # get the app killed instead of answered. A pinned object it doesn't
            # hold yet counts once (its copy is shared).
            need = sum(b.nbytes * (1 if task.ro is not None and b.nbytes >= task.ro else 2)
                       for b in task.packed.buffers)
            need += sum(p_.nbytes for k, p_ in task.packed.pins.items() if k not in node.kept)
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
                    self._missing(v.missing, task, node)
                task.suggestion = v.suggestion
            return False, v.reason
        task.eligible[node.id] = True
        return True, ""

    def _estimate(self, task: Task, node: Node) -> float | None:
        c = self.stats.get(task.fn_key, node.kind)
        if c is None:
            return None
        tc, _ = node.transfer_cost(task)
        if node.state.get("lifecycle") == "background-continued":
            c *= BACKGROUND_SLOWDOWN
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
                if mine is None and node.state.get("lifecycle") == "background-continued" \
                        and any(n.kind == "mac" for n in self.nodes.values()):
                    continue  # no timing yet, and the phone is at a fraction of its speed: not the place to find out
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
        while self._later:  # queued by the scheduler itself, while it was going through the queue
            task, left = self._later.pop(0)
            self.queue.appendleft(task) if left else self.queue.append(task)
            self._qgen += 1
        if self._held:
            self._check_held()
        if any(s.blocked for s in self.sources):
            self._fail_blocked()
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
        if node.kind == "mac" and not task.shadow and not speculative and task.placement in ("auto", None):
            # A free Mac slot took it before any phone looked at it. If a phone
            # couldn't have run it for want of a package, that still counts as a
            # miss, so the package gets offered (or added) either way.
            mods = frozenset(task.packed.modules)
            for n in self.nodes.values():
                if n.kind == "mac" or n.kind in task.excluded:
                    continue
                v = n.verdicts.get(mods)
                if v is None:
                    v = n.verdicts[mods] = router.check(task.packed.modules, n, self.version_policy)
                if not v.ok and v.missing:
                    self._missing(v.missing, task, n)

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
        elif t == "item":  # the next item of a streaming task
            with self._cv:
                att = self.attempts.get(msg.get("id"))
            if att is not None and att.task.stream is not None:
                att.task.stream._put(node, att.id, bufs)
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
        elif t == "approved":
            with self._cv:
                node.held.pop(str(msg.get("code")), None)
                self._qgen += 1
                self._cv.notify_all()
        elif t == "denied":
            with self._cv:
                node.held[str(msg.get("code"))] = "denied"
                self._qgen += 1
                self._cv.notify_all()
        elif t == "pinned" and msg.get("missing") and msg.get("key"):
            self._rekeep(node, str(msg["key"]), list(msg["missing"]))
        elif t == "bye":
            node.bye = str(msg.get("why") or "closing")
        elif t in ("stacks", "installed", "code_log", "pinned", "inventory"):
            ev = self._replies.get(msg.get("req"))
            if ev and not ev[0].is_set():
                ev[1].append(msg)
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
            if u.get("kind") == "object" and task.obj is not None:  # the node doesn't have it (any more)
                task.obj._missing(task, node)
                return
            if task.fallback and u.get("kind") not in ("cache", "pin") and not (
                    task.stream is not None and task.stream.started):
                if u.get("kind") in ("approval", "denied"):
                    code = str(u.get("code") or self._code_of(task)["code"])
                    with self._cv:
                        if node.held.get(code) != "denied":
                            node.held[code] = "waiting" if u.get("kind") == "approval" else "denied"
                self._retarget(task, _unavailable_reason(u, node))
                return
            if u.get("kind") in ("cache", "pin") and task.cache_retries < 2:
                # Its inputs were evicted between arriving and running, or a pinned object it
                # uses isn't on the node (any more): send them again.
                task.cache_retries += 1
                with self._cv:
                    node.known.difference_update(task.packed.hashes)
                    if u.get("kind") == "pin":
                        node.kept.difference_update(task.packed.pins)
                        for p_ in task.packed.pins.values():
                            node.known.difference_update(p_.packed.hashes)
                    if not task.done and not any(a.id in self.attempts for a in task.attempts.values()):
                        task.queued = True
                        self.queue.appendleft(task)
                        self._qgen += 1
                    self._cv.notify_all()
                return
            if u.get("kind") in ("approval", "denied"):
                self._on_held(task, node, u)
                return
            reason = _unavailable_reason(u, node)
            suggestion = None if node.hello.get("fixed_packages") and u.get("kind") == "module" \
                else _unavailable_suggestion(u)
            if u.get("module"):
                node.mac_only_modules.setdefault(u["module"], reason)
                node.verdicts.clear()
                self._missing(u["module"], task, node)
            if task.placement not in ("auto", None) or self.strict:
                return self._fail(task, PlacementError(f"{task.fn_label}: {reason}"
                                                       + (f". {suggestion}" if suggestion else "")))
            # Memory and cache depend on the inputs, not the function; "stopped" was the phone's owner.
            if u.get("kind") not in ("memory", "cache", "stopped", "pin"):
                self.mac_only[task.fn_key] = (reason, suggestion)
                if u.get("module"):  # undone when the module is added to the node
                    self._mac_only_mod[task.fn_key] = u["module"]
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

    def _retarget(self, task: Task, reason: str) -> None:
        """A task placed automatically on a phone that couldn't take it after all: its fallback."""
        with self._cv:
            task.placement, task.fallback, task.reason = task.fallback, None, reason
            task.eligible.clear()
            if not task.done and not task.queued:
                task.queued = True
                self.queue.appendleft(task)
                self._qgen += 1
            self._cv.notify_all()

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
        task.node = node
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

    def _missing(self, package: str, task: Task, node: Node | None = None) -> None:
        """A compiled package kept a task off the phone: add it in the background (or offer to), once."""
        info = envinfo.manifest().get(package)  # a module name, or already a distribution
        dist = info["dist"] if info else package
        if node is not None and node.hello.get("fixed_packages"):
            # The App Store edition's package set is fixed when the app is built: nothing to add.
            with self._cv:
                self.missing_packages[dist] += 1
            if router._norm(dist) not in node.packages:
                self._note(("fixed", dist),
                           f"pocketnode: {dist} isn't in {node.name}'s package set (App Store edition: the set "
                           "is fixed when the app is built), so tasks that need it run on the Mac")
            return
        with self._cv:
            self.missing_packages[dist] += 1
            if dist in self._adding:
                return
            self._adding.add(dist)
        threading.Thread(target=self._add_missing, args=(dist, task), daemon=True, name="pn-add").start()

    def _add_missing(self, dist: str, task: Task) -> None:
        try:
            from .provision import app, hot, packages
        except ImportError:  # provisioning needs packaging/pyyaml
            return
        try:
            packages.record_miss(dist)
            if router._norm(dist) in {router._norm(n) for n in app.config().get("always_mac", [])}:
                return
            est = hot.estimate([dist])
            if not self.auto_add:
                how = ", measured" if est["measured"] else ", estimate"
                self._note(("missing", dist),
                           f"pocketnode: {dist} isn't on the phone ({task.fn_label} needs it).\n"
                           f"  add it without restarting anything (~{est['seconds']} s, {est['mb']} MB{how}): "
                           f"pn.packages.add({dist!r})\n"
                           f"  or keep it on the Mac without asking again: pn.packages.always_mac({dist!r})")
                return
            if est["mb"] > 1500:
                self._note(("missing", dist),
                           f"pocketnode: {dist} isn't on the phone ({task.fn_label} needs it). It is large "
                           f"({est['mb']} MB, about {est['seconds']} s to send): pn.packages.add({dist!r}) adds it")
                return
            try:
                r = hot.add(self, [dist])
            except Exception as e:  # noqa: BLE001
                self._note(("missing", dist), f"pocketnode: couldn't add {dist} to the phone ({e}); "
                                              "tasks that need it run on the Mac")
                return
            if r.get("excluded"):
                parts = "; ".join(f"{m}: {why}" for m, why in list(r["excluded"].items())[:3])
                more = len(r["excluded"]) - 3
                self._note(("excluded", dist), f"pocketnode: parts of {dist} can't run on iOS ({parts}"
                                               + (f"; and {more} more" if more > 0 else "")
                                               + "); tasks using them run on the Mac")
        except Exception:  # noqa: BLE001 - advisory: the task already runs on the Mac
            log.debug("pocketnode: couldn't add %s", dist, exc_info=True)

    def _code_of(self, task: Task) -> dict:
        """What a store phone is told about a task's function: code hash, origin, text (cached)."""
        return self._code_for(task.fn_key, task.fn, task.fn_label)

    def _code_for(self, key: str, fn, label: str) -> dict:
        info = self._src_cache.get(key)
        if info is None:
            info = self._src_cache[key] = _code_info(fn, label)
        return info

    def _on_held(self, task: Task, node: Node, u: dict) -> None:
        """A store phone that asks before running new code held this task back (or it was declined)."""
        code = str(u.get("code") or self._code_of(task)["code"])
        waiting = u.get("kind") == "approval"
        with self._cv:
            if node.held.get(code) != "denied":
                node.held[code] = "waiting" if waiting else "denied"
            self._cv.notify_all()
        auto = task.placement in ("auto", None) and not self.strict
        if waiting:
            then = ("it runs on the Mac until you allow it" if auto else
                    f"it waits up to {APPROVAL_WAIT:.0f} s for you to allow it" if not self.strict else
                    "approve it on the iPhone and submit it again")
            self._note(("approval", code, node.id),
                       f"pocketnode: {task.fn_label} is waiting for approval on {node.name} (Ask before running "
                       f"new code is on there); {then}")
        if auto:
            self._requeue(task, node, _unavailable_reason(u, node), None, quiet=True)
            return
        if not waiting or self.strict:
            self._fail(task, PlacementError(f"{task.fn_label} can't run on {node.label()}: {u.get('detail')}"))
            return
        # Forced there: hold it until the person answers (or APPROVAL_WAIT passes), then send it again.
        with self._cv:
            self._held[task.id] = (task, node.id, code, time.monotonic() + APPROVAL_WAIT)
            if not task.done and not task.queued:
                task.queued = True
                self.queue.appendleft(task)
            self._qgen += 1
            self._cv.notify_all()

    def _fail_blocked(self) -> None:
        """Tasks forced onto a phone that turns this Mac away (paused, not paired, forgotten) fail
        with the reason, once that has held for BLOCKED_GRACE seconds and no such node is connected.
        (With no phone at all, as before, they wait for one.) Under self._cv."""
        now = time.monotonic()
        why = next((s.blocked for s in self.sources
                    if s.blocked and now - s.blocked_since >= BLOCKED_GRACE), None)
        if why is None:
            return
        for t in list(self.queue):
            if t.done or not t.queued or t.placement in ("auto", None, "mac"):
                continue
            if any(_matches(n, t.placement) for n in self.nodes.values()):
                continue
            self._fail(t, PlacementError(f"{t.fn_label} can't run on the {t.placement}: {why}"))

    def _check_held(self) -> None:
        """Forced tasks waiting for approval: release, fail on Don't Allow, or time out. Under self._cv."""
        now = time.monotonic()
        for tid, (task, nid, code, deadline) in list(self._held.items()):
            node = self.nodes.get(nid) or self._gone.get(nid)
            state = node.held.get(code) if node is not None else "waiting"
            if task.done or state is None:  # finished another way, or allowed: it runs normally now
                del self._held[tid]
            elif state == "denied":
                del self._held[tid]
                self._fail(task, PlacementError(
                    f"{task.fn_label} was declined on {node.name if node else 'the phone'} (Don't Allow), "
                    "so it can't run there"))
            elif now > deadline:
                del self._held[tid]
                self._fail(task, PlacementError(
                    f"{task.fn_label} wasn't approved on {node.name if node else 'the phone'} within "
                    f"{APPROVAL_WAIT:.0f} s; approve it on the iPhone and submit it again"))

    def _note(self, key, text: str) -> None:
        if not self.notes:
            return
        if key is not None:
            if key in self._noted:
                return
            self._noted.add(key)
        log.info(text)


def _code_info(fn, label: str = "") -> dict:
    """What a store phone shows for a function: its source (or, when Python has none, its
    disassembly), a hash of that text, where it comes from, and whether it is the user's own."""
    import dis
    import io
    import textwrap
    f = fn
    for _ in range(0 if isinstance(f, type) else 6):  # as in _fn_identity: partials, bound methods, decorators
        inner = (getattr(f, "func", None) or getattr(f, "__func__", None) or getattr(f, "function", None)
                 or getattr(f, "__wrapped__", None))
        if inner is None or not callable(inner):
            break
        f = inner
    try:
        src, kind = textwrap.dedent(inspect.getsource(f)), "source"
    except (OSError, TypeError, IndentationError, SyntaxError):
        buf = io.StringIO()
        try:
            dis.dis(f, file=buf)
        except Exception:  # noqa: BLE001 - a builtin or a C callable: nothing to disassemble
            pass
        src, kind = buf.getvalue(), "bytecode"
        if not src.strip():
            src = f"{label or getattr(f, '__qualname__', type(f).__name__)}: no source or bytecode available\n"
    data = src.encode()
    if len(data) > SRC_LIMIT:
        src = data[:SRC_LIMIT].decode("utf-8", "ignore")
    mod = getattr(f, "__module__", None) or getattr(type(f), "__module__", None) or ""
    info = envinfo.manifest().get(str(mod).split(".")[0]) if mod else None
    co = getattr(f, "__code__", None)
    return {
        "code": hashlib.sha256(src.encode()).hexdigest()[:16],
        "origin": "library" if info else "user",
        "dist": f"{info['dist']} {info['version']}" if info else None,
        "where": f"{os.path.basename(co.co_filename)}:{co.co_firstlineno}" if co is not None else None,
        "src": src, "srckind": kind,
    }


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

    def create(self, factory, /, *args, recreate: bool = True, **kwargs):
        """Like ex.create, on this placement; PlacementError if no node there can hold it."""
        return self._ex._create(factory, args, kwargs, self._p, recreate)

    def stream(self, fn, /, *args, **kwargs):
        """Like ex.stream, on this placement."""
        return self._ex._stream(fn, args, kwargs, self._p)


def _matches(node: Node, placement: str) -> bool:
    return placement in (node.kind, node.name, node.id)


OLDER_APP = "the app on {} is older than this pocketnode; run `pocketnode app install`"


def _lost_error(t: Task, node: Node) -> NodeLost:
    what = f"; {t.obj._label} was kept there" if t.obj is not None else ""
    return NodeLost(f"lost {node.label()} while {t.fn_label} was running there{what}", node=node.name,
                    obj=t.obj._label if t.obj is not None else None)


def _feature_for(task: Task) -> str | None:
    """What a worker must understand to run this task (beyond plain tasks)."""
    if task.stream is not None:
        return "stream"
    if task.kind != "task":
        return "objects"
    if task.packed.pins:
        return "pinned-objects"
    return None


def _lacks(node: Node, feature: str) -> bool:
    """Does this node's worker predate `feature` (its hello doesn't list it)?"""
    return feature not in (node.hello.get("features") or ())


class _Pin:
    """An object ex.pin() keeps on nodes: its pickle, buffers and the key tasks refer to it by."""

    def __init__(self, obj, packed: serialize.Packed, on: str):
        self.obj = obj  # kept alive while pinned: tasks find it by identity
        self.key = uuid.uuid4().hex
        self.packed = packed
        self.modules = set(packed.modules)
        self.nbytes = sum(b.nbytes for b in packed.buffers)
        self.on = on
        self.sig = _versions(obj)
        self.label = type(obj).__qualname__
        self.closed = False
        self.retries: dict[str, int] = {}  # node id -> times its buffers were sent again


def _versions(obj):
    """PyTorch's in-place version counters for a tensor or module (None for anything else):
    a cheap way to tell that a pinned object changed since it was pinned."""
    import sys
    torch = sys.modules.get("torch")
    if torch is None:
        return None
    if isinstance(obj, torch.Tensor):
        return ((id(obj), obj._version),)
    if isinstance(obj, torch.nn.Module):
        return tuple((id(t), t._version) for t in itertools.chain(obj.parameters(), obj.buffers()))
    return None


def _unavailable_reason(u: dict, node: Node) -> str:
    kind = u.get("kind")
    if kind == "module":
        return f"needs {u.get('module')} (compiled), which isn't installed on {node.name}"
    if kind == "mac-file":
        return u["detail"]
    if kind in ("process", "memory", "stopped", "approval", "denied"):
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
