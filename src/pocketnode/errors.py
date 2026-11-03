"""Exceptions shared by the Mac side and workers."""

from __future__ import annotations


class PocketnodeError(Exception):
    pass


class Unavailable(PocketnodeError):
    """The task needs something this node genuinely can't do (a platform limit).

    Raised on a worker, it makes the Mac re-run the task where it can run, unless
    placement was forced, in which case the user sees this error.
    """

    def __init__(self, kind: str, detail: str, module: str | None = None):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.module = module


class PlacementError(PocketnodeError):
    """Forced or strict placement asked for a node that can't run the task."""


class NodeLost(PocketnodeError):
    pass


class TaskCancelled(BaseException):
    """Injected into a worker thread to stop a superseded speculative copy."""


class RemoteTraceback(Exception):
    def __init__(self, tb: str):
        self.tb = tb

    def __str__(self):
        return self.tb
