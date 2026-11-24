"""Content-addressed buffer store on a worker: LRU within a byte budget, plus pins."""

from __future__ import annotations

import threading
from collections import OrderedDict


class BufferStore:
    def __init__(self, budget: int):
        self.budget = budget
        self._bufs: OrderedDict[str, bytearray] = OrderedDict()
        self._pins: dict[str, int] = {}
        self._in_use: dict[str, int] = {}
        self._lock = threading.Lock()
        self.size = 0
        self._evicted: list[str] = []

    def __contains__(self, h: str) -> bool:
        with self._lock:
            return h in self._bufs

    def missing(self, hashes) -> list[str]:
        with self._lock:
            return [h for h in dict.fromkeys(hashes) if h not in self._bufs]

    def put(self, h: str, buf: bytearray) -> None:
        with self._lock:
            if h in self._bufs:
                self._bufs.move_to_end(h)
                return
            self._bufs[h] = buf
            self.size += len(buf)
            self._shrink()

    def reserve(self, hashes, new: dict | None = None) -> list[str]:
        """Add `new` buffers, hold every buffer in `hashes` that is present, and
        return the ones that are missing. Held buffers are never evicted, so a
        task's inputs stay put from the moment they arrive until release()."""
        with self._lock:
            for h, buf in (new or {}).items():
                if h in self._bufs:
                    self._bufs.move_to_end(h)
                else:
                    self._bufs[h] = buf
                    self.size += len(buf)
            missing = []
            for h in hashes:
                if h in self._bufs:
                    self._bufs.move_to_end(h)
                    self._in_use[h] = self._in_use.get(h, 0) + 1
                else:
                    missing.append(h)
            self._shrink()
            return missing

    def get(self, hashes) -> list[bytearray]:
        """Buffers previously held with reserve()."""
        with self._lock:
            return [self._bufs[h] for h in hashes]

    def forget(self, h: str) -> None:
        """Tell the Mac (with the next result) that it can't rely on `h` being here."""
        with self._lock:
            self._evicted.append(h)

    def acquire(self, hashes) -> list[bytearray] | None:
        """Look up and hold buffers for a running task (held ones are never evicted)."""
        with self._lock:
            if any(h not in self._bufs for h in hashes):
                return None
            out = []
            for h in hashes:
                self._bufs.move_to_end(h)
                self._in_use[h] = self._in_use.get(h, 0) + 1
                out.append(self._bufs[h])
            return out

    def release(self, hashes) -> None:
        with self._lock:
            for h in hashes:
                n = self._in_use.get(h, 0) - 1
                if n <= 0:
                    self._in_use.pop(h, None)
                else:
                    self._in_use[h] = n
            self._shrink()

    def pin(self, h: str) -> bool:
        with self._lock:
            if h not in self._bufs:
                return False
            self._pins[h] = self._pins.get(h, 0) + 1
            return True

    def unpin(self, h: str) -> None:
        with self._lock:
            n = self._pins.get(h, 0) - 1
            if n <= 0:
                self._pins.pop(h, None)
            else:
                self._pins[h] = n
            self._shrink()

    def set_budget(self, budget: int) -> None:
        with self._lock:
            self.budget = budget
            self._shrink()

    def take_evicted(self) -> list[str]:
        with self._lock:
            ev, self._evicted = self._evicted, []
            return ev

    def clear(self) -> None:
        with self._lock:
            keep = {h: self._bufs[h] for h in list(self._pins) + list(self._in_use) if h in self._bufs}
            self._evicted.extend(h for h in self._bufs if h not in keep)
            self._bufs = OrderedDict(keep)
            self.size = sum(len(b) for b in keep.values())

    def _shrink(self) -> None:
        if self.size <= self.budget:
            return
        for h in list(self._bufs):
            if self.size <= self.budget:
                break
            if h in self._pins or h in self._in_use:
                continue
            self.size -= len(self._bufs.pop(h))
            self._evicted.append(h)

    def stats(self) -> dict:
        with self._lock:
            return {"n": len(self._bufs), "bytes": self.size, "budget": self.budget, "pinned": len(self._pins)}
