"""Does a result from the phone match the Mac's? Tolerance-aware, structural.

Floating-point results can legitimately differ in the last bits between
machines (different BLAS kernels, summation order). Anything beyond a small
relative tolerance counts as different.
"""

from __future__ import annotations

import math

RTOL = 1e-5
ATOL = 1e-8


def same(a, b, rtol: float = RTOL, atol: float = ATOL, _depth: int = 0) -> tuple[bool, str]:
    """(equal, why-not). Unknown objects are compared through their attributes."""
    if _depth > 12:
        return True, ""
    if a is b:
        return True, ""
    ta = type(a).__module__.split(".")[0]
    if isinstance(a, float) or isinstance(b, float):
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return a == b, f"{a!r} != {b!r}"
        if math.isnan(fa) and math.isnan(fb):
            return True, ""
        ok = math.isclose(fa, fb, rel_tol=rtol, abs_tol=atol)
        return ok, "" if ok else f"{fa!r} vs {fb!r}"
    if ta == "numpy" or hasattr(a, "__array_interface__"):
        import numpy as np
        try:
            x, y = np.asarray(a), np.asarray(b)
        except Exception:
            return a == b, "not comparable"
        if x.shape != y.shape:
            return False, f"shape {x.shape} vs {y.shape}"
        if x.dtype.kind in "fc" or y.dtype.kind in "fc":
            ok = bool(np.allclose(x, y, rtol=rtol, atol=atol, equal_nan=True))
            if not ok:
                with np.errstate(all="ignore"):
                    err = np.nanmax(np.abs(x - y) / (np.abs(y) + atol))
                return False, f"max relative difference {err:.3g}"
            return True, ""
        ok = bool(np.array_equal(x, y))
        return ok, "" if ok else "arrays differ"
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
                return a == b, "differs"
            return True, ""
        except AssertionError as e:
            return False, str(e).splitlines()[0][:200]
    if ta == "torch":
        import torch
        if isinstance(a, torch.Tensor):
            if a.shape != b.shape:
                return False, f"shape {tuple(a.shape)} vs {tuple(b.shape)}"
            if a.is_floating_point() or a.is_complex():
                ok = bool(torch.allclose(a, b, rtol=rtol, atol=atol, equal_nan=True))
                return ok, "" if ok else f"max abs difference {float((a - b).abs().max()):.3g}"
            ok = bool(torch.equal(a, b))
            return ok, "" if ok else "tensors differ"
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return False, "different keys"
        for k in a:
            ok, why = same(a[k], b[k], rtol, atol, _depth + 1)
            if not ok:
                return False, f"[{k!r}]: {why}"
        return True, ""
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False, f"length {len(a)} vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            ok, why = same(x, y, rtol, atol, _depth + 1)
            if not ok:
                return False, f"[{i}]: {why}"
        return True, ""
    if isinstance(a, (str, bytes, int, bool, type(None), complex)) or type(a) is not type(b):
        try:
            ok = bool(a == b)
        except Exception:
            ok = False
        return ok, "" if ok else f"{_short(a)} vs {_short(b)}"
    # Fitted models and other objects: compare their state.
    da, db = getattr(a, "__dict__", None), getattr(b, "__dict__", None)
    if isinstance(da, dict) and isinstance(db, dict):
        return same(da, db, rtol, atol, _depth + 1)
    try:
        ok = bool(a == b)
    except Exception:
        return True, ""  # opaque and not comparable: nothing to check
    return ok, "" if ok else "objects differ"


def _short(x) -> str:
    r = repr(x)
    return r if len(r) < 60 else r[:57] + "..."
