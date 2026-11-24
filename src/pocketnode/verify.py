"""Does a result from the phone match the Mac's? Tolerance-aware, structural.

Floating-point results can legitimately differ in the last bits between
machines (different BLAS kernels, summation order). Anything beyond a small
relative tolerance counts as different.
"""

from __future__ import annotations

import math

RTOL = 1e-5
ATOL = 1e-8


_MAX_DIFFS = 64


def same(a, b, rtol: float = RTOL, atol: float = ATOL) -> tuple[bool, str]:
    """(equal, why-not)."""
    d = diff(a, b, rtol, atol)
    return (not d, f"{d[0][0]}: {d[0][1]}".lstrip(": ") if d else "")


def diff(a, b, rtol: float = RTOL, atol: float = ATOL) -> list[tuple[str, str]]:
    """Every place two results differ, as (path, why). Empty means they match.

    Paths look like "['fit_time']" or "[0].coef_"; "" is the result itself.
    Unknown objects are compared through their attributes.
    """
    out: list[tuple[str, str]] = []
    _walk(a, b, "", rtol, atol, out, 0)
    return out


def outside(diffs: list[tuple[str, str]], unstable: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The differences that aren't explained by places known to vary from run to run."""
    roots = [p for p, _ in unstable]
    return [(p, w) for p, w in diffs if not any(p == r or p.startswith(r) for r in roots)]


def _walk(a, b, path: str, rtol: float, atol: float, out: list, depth: int) -> None:
    if len(out) >= _MAX_DIFFS or depth > 12 or a is b:
        return
    why = _leaf(a, b, path, rtol, atol, out, depth)
    if why:
        out.append((path, why))


def _leaf(a, b, path, rtol, atol, out, depth) -> str | None:
    """Compare one node. Containers recurse (appending to out); a leaf returns why it differs."""
    ta = type(a).__module__.split(".")[0]
    if isinstance(a, bool) or isinstance(b, bool):
        return None if type(a) is type(b) and a == b else f"{_short(a)} vs {_short(b)}"
    if isinstance(a, float) or isinstance(b, float):
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return f"{_short(a)} vs {_short(b)}"
        if (math.isnan(fa) and math.isnan(fb)) or math.isclose(fa, fb, rel_tol=rtol, abs_tol=atol):
            return None
        return f"{fa!r} vs {fb!r}"
    if ta == "numpy" or hasattr(a, "__array_interface__"):
        import numpy as np
        try:
            x, y = np.asarray(a), np.asarray(b)
        except Exception:
            return "not comparable"
        if x.shape != y.shape:
            return f"shape {x.shape} vs {y.shape}"
        if x.dtype.kind == "O" or y.dtype.kind == "O":
            if x.ndim == 0:
                return None if _eq(x.item(), y.item()) else "values differ"
            for i, (u, v) in enumerate(zip(x.ravel().tolist(), y.ravel().tolist())):
                _walk(u, v, f"{path}[{i}]", rtol, atol, out, depth + 1)
            return None
        if x.dtype.kind in "fc" or y.dtype.kind in "fc":
            try:
                if np.allclose(x, y, rtol=rtol, atol=atol, equal_nan=True):
                    return None
                with np.errstate(all="ignore"):
                    err = np.nanmax(np.abs(x - y) / (np.abs(y) + atol))
                return f"max relative difference {err:.3g}"
            except TypeError:
                pass
        return None if np.array_equal(x, y) else "arrays differ"
    if ta == "pandas":
        import pandas as pd
        try:
            if isinstance(a, pd.DataFrame):
                pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=rtol, atol=atol)
            elif isinstance(a, pd.Series):
                pd.testing.assert_series_equal(a, b, check_exact=False, rtol=rtol, atol=atol)
            elif isinstance(a, pd.Index):
                pd.testing.assert_index_equal(a, b, exact=False, check_exact=False, rtol=rtol, atol=atol)
            else:
                return None if _eq(a, b) else "differs"
            return None
        except AssertionError as e:
            return str(e).splitlines()[0][:200]
    if ta == "torch":
        import torch
        if isinstance(a, torch.Tensor):
            if not isinstance(b, torch.Tensor) or a.shape != b.shape:
                return "tensors differ in shape"
            if a.is_floating_point() or a.is_complex():
                if torch.allclose(a, b, rtol=rtol, atol=atol, equal_nan=True):
                    return None
                return f"max abs difference {float((a - b).abs().max()):.3g}"
            return None if torch.equal(a, b) else "tensors differ"
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return "different keys"
        for k in a:
            _walk(a[k], b[k], f"{path}[{k!r}]", rtol, atol, out, depth + 1)
        return None
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return f"length {len(a)} vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            _walk(x, y, f"{path}[{i}]", rtol, atol, out, depth + 1)
        return None
    if isinstance(a, (set, frozenset)) and isinstance(b, (set, frozenset)):
        return None if a == b else "sets differ"
    if isinstance(a, (str, bytes, int, type(None), complex)) or type(a) is not type(b):
        return None if _eq(a, b) else f"{_short(a)} vs {_short(b)}"
    # Fitted models and other objects: compare their state.
    da, db = getattr(a, "__dict__", None), getattr(b, "__dict__", None)
    if isinstance(da, dict) and isinstance(db, dict) and da:
        if da.keys() != db.keys():
            return "different attributes"
        for k in da:
            _walk(da[k], db[k], f"{path}.{k}", rtol, atol, out, depth + 1)
        return None
    if type(a).__eq__ is object.__eq__:
        return None  # opaque and not comparable: nothing to check
    return None if _eq(a, b) else "objects differ"


def _eq(a, b) -> bool:
    try:
        return bool(a == b)
    except Exception:
        return False


def _short(x) -> str:
    r = repr(x)
    return r if len(r) < 60 else r[:57] + "..."
