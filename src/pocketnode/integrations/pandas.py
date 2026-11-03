"""pandas, split across nodes with the usual API: `df.pn.groupby("k").apply(f)`.

Importing pocketnode registers a `.pn` accessor on DataFrame and Series. Each
call returns exactly what the plain pandas call would (same index, same order)
but runs in pieces on the executor. Group-wise operations never split a group.
Whether a piece runs on the Mac or the phone is the scheduler's call, from
measured compute and transfer times, so memory-bound work stays local.
"""

from __future__ import annotations

import math

import pandas as pd


def _ex(executor=None):
    if executor is not None:
        return executor
    from .. import current
    return current()


def _nparts(ex, n: int | None, length: int) -> int:
    if n:
        return max(1, min(n, length))
    slots = sum(nd.effective_slots() for nd in ex.nodes.values()) or 1
    return max(1, min(length, slots * 2))


def _row_chunks(obj, n: int):
    size = math.ceil(len(obj) / n)
    return [obj.iloc[i:i + size] for i in range(0, len(obj), size)]


def _group_chunks(df, by, n: int, groupby_kwargs: dict):
    """Partition rows so each group lands whole in one chunk, balanced by row count."""
    gb = df.groupby(by, **{k: v for k, v in groupby_kwargs.items() if k in ("level", "dropna", "sort")})
    sizes = gb.size()
    target = len(df) / n
    chunks, cur, cur_rows = [], [], 0
    indices = gb.indices
    for key, rows in sizes.items():
        cur.append(indices[key])
        cur_rows += rows
        if cur_rows >= target:
            chunks.append(cur)
            cur, cur_rows = [], 0
    if cur:
        chunks.append(cur)
    import numpy as np
    return [df.iloc[np.sort(np.concatenate(c))] for c in chunks]


def _apply_groups(chunk, by, func, gkw, method, args, kwargs):
    gb = chunk.groupby(by, **gkw)
    return getattr(gb, method)(func, *args, **kwargs)


def _apply_rows(chunk, func, args, kwargs):
    return chunk.apply(func, axis=1, args=args, **kwargs)


def _apply_cols(chunk, func, args, kwargs):
    return chunk.apply(func, axis=0, args=args, **kwargs)


def _call(chunk, func, args, kwargs):
    return func(chunk, *args, **kwargs)


def _series_map(chunk, func, na_action):
    return chunk.map(func, na_action=na_action)


class PocketGroupBy:
    def __init__(self, df, by, executor, npartitions, groupby_kwargs):
        self._df, self._by, self._ex, self._n, self._gkw = df, by, executor, npartitions, groupby_kwargs

    def _run(self, method, func, args, kwargs, combine):
        ex = _ex(self._ex)
        chunks = _group_chunks(self._df, self._by, _nparts(ex, self._n, max(1, self._df.shape[0])), self._gkw)
        futs = [ex.submit(_apply_groups, c, self._by, func, self._gkw, method, args, kwargs) for c in chunks]
        return combine([f.result() for f in futs])

    def apply(self, func, *args, **kwargs):
        sort = self._gkw.get("sort", True)

        def combine(parts):
            out = pd.concat(parts)
            if sort:  # chunks hold disjoint groups; restore pandas' sorted group order
                lv = list(range(_nkeys(self._by)))
                try:
                    out = out.sort_index(level=lv, sort_remaining=False, kind="stable")
                except TypeError:
                    pass
            return out
        return self._run("apply", func, args, kwargs, combine)

    def agg(self, func, *args, **kwargs):
        return self.apply_agg("agg", func, args, kwargs)

    aggregate = agg

    def apply_agg(self, method, func, args, kwargs):
        sort = self._gkw.get("sort", True)

        def combine(parts):
            out = pd.concat(parts)
            return out.sort_index(kind="stable") if sort else out
        return self._run(method, func, args, kwargs, combine)

    def transform(self, func, *args, **kwargs):
        # transform keeps the original row index; put rows back in original order.
        return self._run("transform", func, args, kwargs, lambda parts: pd.concat(parts).loc[self._df.index])


def _nkeys(by) -> int:
    return len(by) if isinstance(by, (list, tuple)) else 1


@pd.api.extensions.register_dataframe_accessor("pn")
class DataFrameAccessor:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def groupby(self, by, *, executor=None, npartitions=None, **groupby_kwargs) -> PocketGroupBy:
        return PocketGroupBy(self._df, by, executor, npartitions, groupby_kwargs)

    def apply(self, func, axis=0, args=(), *, executor=None, npartitions=None, **kwargs):
        ex = _ex(executor)
        if axis in (1, "columns"):
            parts = _row_chunks(self._df, _nparts(ex, npartitions, len(self._df)))
            futs = [ex.submit(_apply_rows, p, func, args, kwargs) for p in parts]
            return pd.concat([f.result() for f in futs])
        cols = list(self._df.columns)
        n = _nparts(ex, npartitions, len(cols))
        size = math.ceil(len(cols) / n)
        futs = [ex.submit(_apply_cols, self._df[cols[i:i + size]], func, args, kwargs) for i in range(0, len(cols), size)]
        parts = [f.result() for f in futs]
        return pd.concat(parts, axis=1 if isinstance(parts[0], pd.DataFrame) else 0)

    def map_partitions(self, func, *args, executor=None, npartitions=None, **kwargs):
        """func(chunk) on row chunks, results concatenated (like dask's map_partitions)."""
        ex = _ex(executor)
        parts = _row_chunks(self._df, _nparts(ex, npartitions, len(self._df)))
        futs = [ex.submit(_call, p, func, args, kwargs) for p in parts]
        return pd.concat([f.result() for f in futs])


@pd.api.extensions.register_series_accessor("pn")
class SeriesAccessor:
    def __init__(self, s: pd.Series):
        self._s = s

    def map(self, func, na_action=None, *, executor=None, npartitions=None):
        ex = _ex(executor)
        parts = _row_chunks(self._s, _nparts(ex, npartitions, len(self._s)))
        futs = [ex.submit(_series_map, p, func, na_action) for p in parts]
        return pd.concat([f.result() for f in futs])

    def apply(self, func, args=(), *, executor=None, npartitions=None, **kwargs):
        ex = _ex(executor)
        parts = _row_chunks(self._s, _nparts(ex, npartitions, len(self._s)))
        futs = [ex.submit(_call, p, lambda c, *a, **k: c.apply(func, args=a, **k), args, kwargs) for p in parts]
        return pd.concat([f.result() for f in futs])

    def map_partitions(self, func, *args, executor=None, npartitions=None, **kwargs):
        ex = _ex(executor)
        parts = _row_chunks(self._s, _nparts(ex, npartitions, len(self._s)))
        futs = [ex.submit(_call, p, func, args, kwargs) for p in parts]
        return pd.concat([f.result() for f in futs])
