"""pocketnode: your iPhone as a compute node for your Mac.

    import pocketnode as pn
    ex = pn.connect()                 # a concurrent.futures.Executor
    fut = ex.submit(fit, X, y)
    fut.result(); fut.placement       # where it ran and why

    with pn.parallel():               # joblib / scikit-learn n_jobs=-1
        GridSearchCV(model, grid, n_jobs=-1).fit(X, y)

Placement is automatic and can be overridden anywhere:
    ex.on("phone").submit(f, x)       # one call
    @pn.runs_on("mac")                # one function
    with pn.placement("phone"): ...   # a block
    pn.connect(placement="mac")       # everything
"""

from __future__ import annotations

import logging
import os

from .errors import NodeLost, PlacementError, Unavailable
from .executor import Placement, PocketExecutor, PocketFuture, placement, runs_on

__all__ = ["connect", "current", "parallel", "placement", "runs_on", "PocketExecutor", "PocketFuture",
           "Placement", "PlacementError", "Unavailable", "NodeLost"]

_current: PocketExecutor | None = None


def _register_pandas(_mod) -> None:
    from .integrations import pandas as _  # noqa: F401  (registers the .pn accessor)


from .compat import when_imported as _when_imported  # noqa: E402

_when_imported("pandas", _register_pandas)


def _setup_logging() -> None:
    log = logging.getLogger("pocketnode")
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False


def connect(*addresses: str, mac_workers: int | None = None, phone: bool = True, udid: str | None = None,
            placement: str = "auto", strict: bool = False, version_policy: str = "minor",
            speculate: bool = True, notes: bool = True, wait: float | None = None,
            simulate_phone: bool | str = False) -> PocketExecutor:
    """Start an executor that pools this Mac's cores and the phone.

    addresses: extra workers at "host:port" (e.g. the phone over Wi-Fi).
    mac_workers: Mac worker processes (default: one per core; 0 for phone only).
    placement: "auto", "mac", "phone" or a node name. Overridable per call/function/block.
    strict: raise instead of falling back when a task can't run where it was routed.
    wait: seconds to wait for the phone to connect before returning.
    simulate_phone: run a local stand-in that behaves like the phone (a path to
        a Python interpreter whose environment mimics the phone's, or True).
    """
    global _current
    from .sources import Address, LocalWorkers, PhoneLink

    _setup_logging()
    ex = PocketExecutor(placement=placement, strict=strict, version_policy=version_policy,
                        speculate=speculate, notes=notes)
    n = os.cpu_count() if mac_workers is None else mac_workers
    if n:
        ex.add_source(LocalWorkers(n))
    if phone:
        ex.add_source(PhoneLink(udid=udid))
    for a in addresses:
        ex.add_source(Address(a))
    if simulate_phone:
        from .sources import SimulatedPhone
        ex.add_source(SimulatedPhone(python=simulate_phone if isinstance(simulate_phone, str) else None))
    if wait:
        ex.wait_for("phone", wait)
    _current = ex
    return ex


def current() -> PocketExecutor:
    """The executor from the last connect(), connecting with defaults if there is none."""
    global _current
    if _current is None:
        connect()
    return _current


def parallel(executor: PocketExecutor | None = None, **kwargs):
    """joblib context: `with pn.parallel(): GridSearchCV(..., n_jobs=-1).fit(X, y)`."""
    from .integrations.joblib_backend import parallel as _p
    return _p(executor or current(), **kwargs)


def load_ipython_extension(ip):
    from .integrations.ipython import load_ipython_extension as _load
    _load(ip)
