"""Objects kept on a node: `shard = ex.on("phone").create(ShardRunner, weights)`.

The object is built on one node and stays there; `shard.forward(x)` runs that method on it
and returns a future. The handle sends one call at a time, in order (an object isn't assumed
to be thread-safe), so a call that is waiting its turn takes no slot on the node.

If the node goes away, calls in flight fail with NodeLost. The next call rebuilds the object
from its constructor and arguments first, on the same node if it came back with the object
still there, else on a node of the same kind (unless recreate=False).
"""

from __future__ import annotations

import hashlib
import inspect
import threading
import uuid
from collections import deque

from . import serialize
from .errors import NodeLost, PlacementError
from .executor import PocketExecutor, PocketFuture, Task, _fn_identity

_DROP = object()  # in the call queue: drop the object on its node once the calls before it finish


def _identity(factory) -> tuple[str, str]:
    """(stats/code key, label) for a class or a factory function."""
    if not isinstance(factory, type):
        return _fn_identity(factory)
    try:
        h = hashlib.sha1(inspect.getsource(factory).encode()).hexdigest()[:12]
    except (OSError, TypeError):
        h = f"{id(factory):x}"
    return f"{factory.__module__}.{factory.__qualname__}:{h}", factory.__qualname__


def _method_of(factory, name: str):
    """The function behind `name` on a class, for its source; None if it can't be told on the Mac."""
    if not isinstance(factory, type):
        return None
    try:
        f = inspect.getattr_static(factory, name)
    except AttributeError:
        return None
    f = getattr(f, "__func__", f)  # staticmethod, classmethod
    return f if inspect.isfunction(f) else None


class RemoteObject:
    """A handle to an object kept on one node. Public attribute names are its methods."""

    def __init__(self, ex: PocketExecutor, factory, args, kwargs, where: str, recreate: bool):
        self._ex = ex
        self._id = uuid.uuid4().hex
        self._factory = factory
        self._ctor = (factory, args, kwargs)  # kept to rebuild it (and dropped if that's off)
        self._where = where
        self._recreate = recreate
        self._key, self._label = _identity(factory)
        self._node = None  # executor.Node holding it
        # new | live | lost (its node went away; may come back with it) | gone (rebuild it) | failed
        self._state = "new"
        self._closed = False
        self._gen = 0  # times its node was lost
        self._lock = threading.RLock()
        self._fifo: deque = deque()
        self._current: Task | None = None
        self._rebuilt = False

    # ------------------------------------------------------------ public

    @property
    def node(self) -> str | None:
        """The name of the node holding it."""
        return self._node.name if self._node is not None else None

    @property
    def kind(self) -> str | None:
        """"phone" or "mac"."""
        return self._node.kind if self._node is not None else None

    def close(self) -> None:
        """Drop it on its node (after the calls already made have run)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._fifo.append(_DROP)
        self._ex._objects.pop(self._id, None)
        self._pump()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return _Method(self, name)

    def __repr__(self):
        where = f" on {self._node.name}" if self._node is not None else ""
        return f"<RemoteObject {self._label}{where}{' (closed)' if self._closed else ''}>"

    def __reduce__(self):
        raise TypeError(f"{self!r} is a handle to an object kept on a node; pass it values, "
                        "or call its methods from the Mac")

    # ------------------------------------------------------------ internals

    def _start(self) -> None:
        task = self._ctor_task()
        if self._where in ("auto", None):
            self._ex._place_once(task)
        else:
            task.placement = self._where
        with self._lock:
            self._current = task
        self._ex._objects[self._id] = self
        self._launch(task)
        try:
            task.future.result()
        except BaseException:
            self._ex._objects.pop(self._id, None)
            raise
        if not self._recreate:
            self._ctor = None

    def _ctor_task(self) -> Task:
        ex = self._ex
        packed = serialize.dumps(self._ctor, pins=ex._pin_of if ex._kept else None)
        t = Task(uuid.uuid4().hex, self._key, self._label, packed, PocketFuture(), "auto", fn=self._factory,
                 kind="create", obj=self, sticky=True, gen=self._gen)
        t.reason = f"{self._label} is kept there"
        return t

    def _call(self, name: str, args, kwargs, stream: bool = False):
        with self._lock:
            if self._closed:
                raise RuntimeError(f"{self!r} is closed")
        ex = self._ex
        if ex._shutdown:
            raise RuntimeError("cannot call methods after shutdown")
        packed = serialize.dumps((args, kwargs), pins=ex._pin_of if ex._kept else None)
        fn = _method_of(self._factory, name)
        t = Task(uuid.uuid4().hex, f"{self._key}.{name}", f"{self._label}.{name}", packed, PocketFuture(),
                 "auto", fn=fn or self._factory, kind="call", obj=self, method=name, sticky=True)
        t.reason = f"where {self._label} is kept"
        it = _streaming(ex, t) if stream else None
        with self._lock:
            t.gen = self._gen
            self._fifo.append(t)
        self._pump()
        return it if stream else t.future

    def _launch(self, t: Task) -> None:
        if getattr(t, "_admitted", False):
            self._ex._requeue_task(t)
            return
        t._admitted = True
        self._ex._admit(t)
        t.future.add_done_callback(lambda f, t=t: self._done(t))

    def _pump(self) -> None:
        """Send the next call (or rebuild first) once the one before it is done."""
        while True:
            fail = drop = None
            with self._lock:
                if self._current is not None or not self._fifo:
                    return
                t = self._fifo[0]
                if t is _DROP:
                    self._fifo.popleft()
                    drop = self._node
                elif t.future.done():  # cancelled before its turn
                    self._fifo.popleft()
                    continue
                elif t.gen != self._gen:  # made before its node was lost
                    self._fifo.popleft()
                    fail = NodeLost(f"{t.fn_label} wasn't run: {self._node.label()} went away, and "
                                    f"{self._label} with it", node=self._node.name, obj=self._label)
                elif self._state == "live":
                    self._fifo.popleft()
                    t.placement = self._node.id
                    self._current = t
                elif self._state in ("lost", "gone"):
                    back = self._ex.nodes.get(self._node.id) if self._state == "lost" else None
                    if back is not None and back.connected:  # the same worker: it may still have it
                        self._fifo.popleft()
                        self._node, self._state = back, "live"
                        t.placement = back.id
                        self._current = t
                    elif self._ctor is None:
                        self._fifo.popleft()
                        fail = NodeLost(f"{self._label} was on {self._node.label()}, which went away; it was "
                                        "created with recreate=False, so it isn't rebuilt",
                                        node=self._node.name, obj=self._label)
                    else:
                        t = self._ctor_task()
                        t.placement = self._node.kind if self._where in ("auto", None) else self._where
                        self._rebuilt = True
                        self._current = t
                else:
                    self._fifo.popleft()
                    fail = RuntimeError(f"{self._label} couldn't be built")
            if drop is not None:
                if self._ex.nodes.get(drop.id) is drop:
                    drop.post({"t": "drop", "obj": self._id})
                continue
            if fail is not None:
                self._ex._fail(t, fail)
                continue
            self._launch(t)
            return

    def _done(self, t: Task) -> None:
        note = nxt = exc = None
        with self._lock:
            if self._current is t:
                self._current = None
            if t.kind == "create":
                failed = t.future.cancelled() or t.future.exception() is not None
                if not failed and t.node is not None:
                    self._node, self._state = t.node, "live"
                    if self._rebuilt:
                        self._rebuilt = False
                        note = (f"pocketnode: {self._label} was rebuilt on {t.node.name} from its constructor "
                                "arguments; any state it had built up is gone")
                elif self._state == "new":
                    self._state = "failed"
                elif self._fifo and self._fifo[0] is not _DROP:
                    # Rebuilding failed: the call it was for gets the reason.
                    nxt = self._fifo.popleft()
                    exc = NodeLost("rebuilding was cancelled") if t.future.cancelled() else t.future.exception()
        if note:
            self._ex._note(None, note)
        if nxt is not None:
            self._ex._fail(nxt, exc)
        self._pump()

    def _lost(self, node) -> None:
        """Its node went away (called by the executor, under its lock)."""
        with self._lock:
            if self._node is None or self._node.id != node.id or self._state != "live":
                return
            self._state = "lost"
            self._gen += 1
            doomed = [t for t in self._fifo if t is not _DROP]
            self._fifo = deque(t for t in self._fifo if t is _DROP)
        for t in doomed:
            self._ex._fail(t, NodeLost(f"{t.fn_label} wasn't run: lost {node.label()}, which held {self._label}",
                                       node=node.name, obj=self._label))

    def _missing(self, t: Task, node) -> None:
        """The node answered that it doesn't have the object (its app restarted, say)."""
        with self._lock:
            if self._current is t:
                self._current = None
            if self._node is not None and self._node.id == node.id:
                self._state = "gone"
            self._fifo.appendleft(t)  # run it once the object is back
        if self._ctor is None:
            with self._lock:
                self._fifo.popleft()
            self._ex._fail(t, NodeLost(f"{self._label} isn't on {node.label()} any more (it was created with "
                                       "recreate=False, so it isn't rebuilt)", node=node.name, obj=self._label))
        self._pump()


class _Method:
    """`handle.name`: call it to run the method on the kept object."""

    def __init__(self, obj: RemoteObject, name: str):
        self._obj, self._name = obj, name

    def __call__(self, *args, **kwargs) -> PocketFuture:
        return self._obj._call(self._name, args, kwargs)

    def stream(self, *args, **kwargs) -> "RemoteStream":
        """Call a method that yields, and iterate over its items as they are produced."""
        return self._obj._call(self._name, args, kwargs, stream=True)

    def __repr__(self):
        return f"<method {self._name} of {self._obj!r}>"


class _StreamState:
    """What has arrived of a streaming task (task.stream). The iterator the user holds is
    separate, so dropping that iterator can stop the task while the executor still holds this."""

    def __init__(self):
        self.items: deque = deque()
        self.cv = threading.Condition()
        self.started = False  # an item has arrived
        self.closed = False
        self.max_buffered = 0  # most items ever waiting on the Mac at once

    def _put(self, node, att: str, bufs) -> None:
        with self.cv:
            if self.closed:
                return
            self.started = True
            self.items.append((node, att, bufs))
            self.max_buffered = max(self.max_buffered, len(self.items))
            self.cv.notify_all()

    def wake(self, _f=None) -> None:
        with self.cv:
            self.cv.notify_all()


class RemoteStream:
    """The items a generator on a node yields, in order, as they arrive (ex.stream, method.stream).

    Iterating takes the next item (waiting for it if need be); the iterator ends when the
    generator does and raises what it raised. close(), or dropping the iterator, stops the
    generator on the node. `placement` says where it ran once it has finished.
    """

    def __init__(self, ex: PocketExecutor, task: Task):
        from .executor import STREAM_ACK
        self._ex, self._task = ex, task
        self._s = task.stream
        self._taken = self._acked = 0
        self._ack_every = STREAM_ACK
        self._done = False
        task.future.add_done_callback(self._s.wake)

    @property
    def placement(self):
        return self._task.future.placement

    @property
    def max_buffered(self) -> int:
        """The most items that were ever waiting here for you at once."""
        return self._s.max_buffered

    def __iter__(self):
        return self

    def __next__(self):
        s, f = self._s, self._task.future
        with s.cv:
            while not s.items and not f.done():
                s.cv.wait(0.5)
            item = s.items.popleft() if s.items else None
        if item is None:  # finished, and everything it sent has been taken
            if self._done or f.cancelled():
                raise StopIteration
            self._done = True
            exc = f.exception()
            if exc is not None:
                raise exc
            raise StopIteration
        node, att, bufs = item
        self._taken += 1
        if self._taken - self._acked >= self._ack_every and not f.done():
            self._acked = self._taken
            node.post({"t": "ack", "id": att, "n": self._taken})
        return serialize.loads(bufs[0], bufs[1:])

    def close(self) -> None:
        """Stop the generator on its node (if it is still running) and end the iteration."""
        s = self._s
        with s.cv:
            if s.closed:
                return
            s.closed = True
            s.items.clear()
        self._done = True
        if not self._task.future.done():
            from concurrent.futures import CancelledError
            self._ex._fail(self._task, CancelledError("the stream was closed"))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 - at interpreter exit
            pass


def _streaming(ex: PocketExecutor, task: Task) -> RemoteStream:
    task.stream = _StreamState()
    return RemoteStream(ex, task)


__all__ = ["RemoteObject", "RemoteStream", "PlacementError"]
