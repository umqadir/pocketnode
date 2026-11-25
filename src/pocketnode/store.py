"""Content-addressed buffer store on a worker: LRU within a byte budget, plus pins.

Buffers of DISK_MIN bytes or more, and pinned ones, are also written to disk
under their content hash, so large inputs (model weights, say) survive an app
restart. Memory and disk have their own budgets and are each evicted least
recently used first; evicting from memory keeps the file, and a buffer missing
from memory is read back from its file (and checked against its hash).
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from .wire import digest

DISK_MIN = 8 << 20  # buffers this big or bigger are also kept on disk
DISK_SHARE = 0.2    # default disk budget: this share of the volume's free space at startup...
DISK_CAP = 50 << 30  # ...but no more than this
CACHED_REPORT = 4096  # hashes on disk reported to a Mac when it connects
_HASH = re.compile(r"^[0-9a-f]{32}$")


class BufferStore:
    def __init__(self, budget: int, disk_dir: str | os.PathLike | None = None, disk_budget: int | None = None):
        self.budget = budget
        self._bufs: OrderedDict[str, bytearray] = OrderedDict()
        self._pins: dict[str, int] = {}
        self._in_use: dict[str, int] = {}
        self._lock = threading.Lock()
        self.size = 0
        self._evicted: list[str] = []
        # The disk tier: hash -> bytes, least recently used first.
        self.disk_dir: Path | None = None
        self._disk: OrderedDict[str, int] = OrderedDict()
        self._writing: set[str] = set()  # queued for the writer thread, not on disk yet
        self._touched: dict[str, float] = {}
        self.disk_size = 0
        self.disk_budget = 0
        if disk_dir is not None:
            self._open_disk(Path(disk_dir), disk_budget)

    # ------------------------------------------------------------- disk

    def _open_disk(self, d: Path, budget: int | None) -> None:
        try:
            d.mkdir(parents=True, exist_ok=True)
            entries = list(os.scandir(d))
        except OSError:
            return
        found = []
        for e in entries:
            try:
                if _HASH.match(e.name) and e.is_file():
                    st = e.stat()
                    found.append((st.st_atime, e.name, st.st_size))
                elif ".tmp-" in e.name:  # a write that never finished
                    os.unlink(e.path)
            except OSError:
                continue
        for _, h, n in sorted(found):
            self._disk[h] = n
            self.disk_size += n
        if budget is None:
            try:
                free = shutil.disk_usage(d).free
            except OSError:
                free = 0
            # Free space as the cache sees it: what is free plus what it already holds.
            budget = min(int((free + self.disk_size) * DISK_SHARE), DISK_CAP)
        self.disk_dir = d
        self.disk_budget = budget
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._writer, daemon=True, name="pn-disk").start()
        with self._lock:
            self._shrink_disk()

    def _persist(self, h: str, buf, force: bool = False) -> None:
        """Queue a buffer for its file (under self._lock)."""
        if self.disk_dir is None or "@" in h or h in self._disk or h in self._writing:
            return
        if (len(buf) < DISK_MIN and not force) or len(buf) > self.disk_budget:
            return
        self._writing.add(h)
        self._q.put((h, buf))

    def _writer(self) -> None:
        while True:
            h, buf = self._q.get()
            try:
                ok = self._write(h, buf)
                with self._lock:
                    self._writing.discard(h)
                    if ok and h not in self._disk:
                        self._disk[h] = len(buf)
                        self.disk_size += len(buf)
                        self._shrink_disk()
                    elif not ok and h not in self._bufs:
                        self._evicted.append(h)
            finally:
                self._q.task_done()

    def _write(self, h: str, buf) -> bool:
        tmp = self.disk_dir / f"{h}.tmp-{uuid.uuid4().hex[:8]}"
        try:
            with open(tmp, "wb") as f:
                f.write(buf)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.disk_dir / h)
            return True
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    def _load(self, h: str) -> bytearray | None:
        """Read a buffer back from its file (not under self._lock). None if it's gone or damaged."""
        p = self.disk_dir / h
        try:
            buf = bytearray(os.path.getsize(p))
            with open(p, "rb", buffering=0) as f:
                view, got = memoryview(buf), 0
                while got < len(buf):
                    n = f.readinto(view[got:])
                    if not n:
                        raise OSError("file shorter than it was")
                    got += n
        except OSError:
            self._drop_file(h, unlink=False)
            return None
        if digest(buf) != h:
            self._drop_file(h, unlink=True)
            return None
        return buf

    def _drop_file(self, h: str, unlink: bool) -> None:
        with self._lock:
            n = self._disk.pop(h, None)
            if n is not None:
                self.disk_size -= n
            if h not in self._bufs:
                self._evicted.append(h)
        if unlink:
            try:
                os.unlink(self.disk_dir / h)
            except OSError:
                pass

    def _touch(self, h: str) -> None:
        """A use of `h` counts for the disk tier's LRU too, across restarts (under self._lock)."""
        if h not in self._disk:
            return
        self._disk.move_to_end(h)
        now = time.monotonic()
        if now - self._touched.get(h, -1e9) > 60:
            self._touched[h] = now
            try:
                os.utime(self.disk_dir / h)
            except OSError:
                pass

    def _shrink_disk(self) -> None:
        if self.disk_size <= self.disk_budget:
            return
        for h in list(self._disk):
            if self.disk_size <= self.disk_budget:
                break
            if h in self._pins:
                continue
            self.disk_size -= self._disk.pop(h)
            self._touched.pop(h, None)
            try:
                os.unlink(self.disk_dir / h)
            except OSError:
                pass
            if h not in self._bufs:
                self._evicted.append(h)

    def _gone(self, h: str) -> bool:
        """Evicted from memory: does the Mac need to hear it (no file to read it back from)?"""
        return h not in self._disk and h not in self._writing

    def cached(self, limit: int = CACHED_REPORT) -> list[str]:
        """Hashes held on disk, most recently used first."""
        with self._lock:
            return list(reversed(self._disk))[:limit]

    def flush(self) -> None:
        """Wait until every queued file is written."""
        if self.disk_dir is not None:
            self._q.join()

    # ----------------------------------------------------------- memory

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
            self._persist(h, buf)
            self._shrink()

    def reserve(self, hashes, new: dict | None = None) -> list[str]:
        """Add `new` buffers, hold every buffer in `hashes` that is present (in memory, or
        read back from disk), and return the ones that are missing. Held buffers are never
        evicted, so a task's inputs stay put from the moment they arrive until release()."""
        with self._lock:
            for h, buf in (new or {}).items():
                if h in self._bufs:
                    self._bufs.move_to_end(h)
                else:
                    self._bufs[h] = buf
                    self.size += len(buf)
                    self._persist(h, buf)
            missing = []
            for h in hashes:
                if h in self._bufs:
                    self._bufs.move_to_end(h)
                    self._in_use[h] = self._in_use.get(h, 0) + 1
                    self._touch(h)
                else:
                    missing.append(h)
            on_disk = [h for h in dict.fromkeys(missing) if h in self._disk]
            if not on_disk:
                self._shrink()
                return missing
        loaded = {}
        for h in on_disk:  # outside the lock: reading a large file takes a while
            b = self._load(h)
            if b is not None:
                loaded[h] = b
        with self._lock:
            still = []
            for h in missing:
                if h not in self._bufs and h in loaded:
                    self._bufs[h] = loaded.pop(h)
                    self.size += len(self._bufs[h])
                if h in self._bufs:
                    self._bufs.move_to_end(h)
                    self._in_use[h] = self._in_use.get(h, 0) + 1
                    self._touch(h)
                else:
                    still.append(h)
            self._shrink()
            return still

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
        """Keep `h` in memory (read back from disk if it was evicted) and on disk."""
        with self._lock:
            if h in self._bufs:
                self._pins[h] = self._pins.get(h, 0) + 1
                self._persist(h, self._bufs[h], force=True)
                return True
            if h not in self._disk:
                return False
        b = self._load(h)
        if b is None:
            return False
        with self._lock:
            if h not in self._bufs:
                self._bufs[h] = b
                self.size += len(b)
            self._pins[h] = self._pins.get(h, 0) + 1
            self._touch(h)
            self._shrink()
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
        """Empty memory (a memory warning); files stay."""
        with self._lock:
            keep = {h: self._bufs[h] for h in list(self._pins) + list(self._in_use) if h in self._bufs}
            self._evicted.extend(h for h in self._bufs if h not in keep and self._gone(h))
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
            if self._gone(h):
                self._evicted.append(h)

    def stats(self) -> dict:
        with self._lock:
            return {"n": len(self._bufs), "bytes": self.size, "budget": self.budget, "pinned": len(self._pins),
                    "disk_n": len(self._disk), "disk_bytes": self.disk_size, "disk_budget": self.disk_budget}
