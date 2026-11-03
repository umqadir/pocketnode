"""joblib backend: `with pn.parallel(): GridSearchCV(est, grid, n_jobs=-1).fit(X, y)`.

Batches go through the pocketnode scheduler, so they spread over Mac cores and
the phone with the same placement rules as `executor.submit`. Arrays of 1 MB
and more reach tasks read-only, exactly as joblib's own process backend
memory-maps them, and cross the link once per Parallel call however many
batches use them.
"""

from __future__ import annotations

import contextlib

from joblib import parallel_config, register_parallel_backend
from joblib._parallel_backends import AutoBatchingMixin, ParallelBackendBase

from .. import serialize


class PocketBackend(AutoBatchingMixin, ParallelBackendBase):
    supports_retrieve_callback = True
    supports_timeout = True
    default_n_jobs = -1  # inside pn.parallel(), n_jobs=None means "use everything"

    def __init__(self, executor=None, max_nbytes: int | str | None = "1M", **kwargs):
        super().__init__(**kwargs)
        self.executor = executor
        self.ro_min = _parse_bytes(max_nbytes)
        self._memo = None
        self._futures: set = set()

    def _ex(self):
        if self.executor is None:
            from .. import current
            self.executor = current()
        return self.executor

    def effective_n_jobs(self, n_jobs):
        if n_jobs == 0:
            raise ValueError("n_jobs == 0 in Parallel has no meaning")
        total = max(1, sum(n.effective_slots() for n in self._ex().nodes.values())
                    or (__import__("os").cpu_count() or 1))
        if n_jobs is None or n_jobs < 0:
            return max(1, total + 1 + (n_jobs if n_jobs is not None else -1))
        return n_jobs

    def configure(self, n_jobs=1, parallel=None, **kwargs):
        self.parallel = parallel
        self._memo = serialize.HashMemo()  # each X is hashed once per Parallel call
        return self.effective_n_jobs(n_jobs)

    def submit(self, func, callback=None):
        fut = self._ex()._submit(func, (), {}, None, self._memo, ro=self.ro_min)
        self._futures.add(fut)
        fut.add_done_callback(self._futures.discard)
        if callback is not None:
            fut.add_done_callback(callback)
        return fut

    def retrieve_result_callback(self, future):
        return future.result()

    def retrieve_result(self, out, timeout=None):
        return out.result(timeout=timeout)

    def abort_everything(self, ensure_ready=True):
        for f in list(self._futures):
            f.cancel()
        self._futures.clear()

    def terminate(self):
        self._memo = None
        self.reset_batch_stats()

    def get_nested_backend(self):
        from joblib._parallel_backends import ThreadingBackend
        return ThreadingBackend(nesting_level=self.nesting_level + 1), None


def _parse_bytes(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    v = v.strip().upper()
    return int(float(v[:-1]) * units[v[-1]]) if v[-1] in units else int(v)


@contextlib.contextmanager
def parallel(executor, n_jobs=None, **kwargs):
    register_parallel_backend("pocketnode", lambda **kw: PocketBackend(executor, **kw))
    with parallel_config(backend="pocketnode", n_jobs=n_jobs, **kwargs):
        yield executor
