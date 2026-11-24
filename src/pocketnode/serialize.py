"""Pickling with out-of-band buffers, content addressing and module discovery."""

from __future__ import annotations

import dis
import io
import pickle
import pickletools
import sys
import sysconfig
import types
from dataclasses import dataclass, field

import cloudpickle

from .wire import as_bytes_view, digest

# Buffers smaller than this ride inside the pickle; larger ones go out-of-band
# and are content-addressed so they cross the link once.
OOB_MIN = 64 * 1024


@dataclass
class Packed:
    data: bytes
    buffers: list[memoryview] = field(default_factory=list)
    hashes: list[str] = field(default_factory=list)
    modules: set[str] = field(default_factory=set)

    @property
    def nbytes(self) -> int:
        return len(self.data) + sum(b.nbytes for b in self.buffers)


class HashMemo:
    """Hash each distinct object once per dispatch batch (a map() or a joblib call).

    Keyed on the exporting object's identity and size. The object is kept alive
    for the batch, so its id can't be recycled within it.
    """

    def __init__(self):
        self._memo: dict[tuple, str] = {}
        self._keep: list = []

    def get(self, view: memoryview) -> str:
        obj = view.obj
        key = (id(obj), view.nbytes)
        h = self._memo.get(key)
        if h is None:
            h = self._memo[key] = digest(view)
            self._keep.append(obj)
        return h


_user_modules_registered: set[str] = set()
_STDLIB_DIRS = {sysconfig.get_paths()["stdlib"], sysconfig.get_paths()["platstdlib"]}
_SITE_DIRS = {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}


def is_user_module(mod: types.ModuleType) -> bool:
    """A module of the user's own (not stdlib, not installed in site-packages)."""
    name = mod.__name__
    if name == "__main__" or name.split(".")[0] in sys.stdlib_module_names:
        return False
    f = getattr(mod, "__file__", None)
    if not f or not f.endswith(".py"):
        return False
    if any(f.startswith(d) for d in _STDLIB_DIRS | _SITE_DIRS):
        return False
    if "site-packages" in f or "dist-packages" in f:
        return False
    return not name.startswith("pocketnode")


_seen_module_count = 0


def register_user_modules() -> None:
    """Pickle the user's own modules by value.

    Workers then run exactly the code the Mac has loaded right now, including
    edits picked up by autoreload, instead of whatever is on disk.
    """
    global _seen_module_count
    if len(sys.modules) == _seen_module_count:
        return
    _seen_module_count = len(sys.modules)
    for name, mod in list(sys.modules.items()):
        if name in _user_modules_registered or mod is None:
            continue
        try:
            if is_user_module(mod):
                cloudpickle.register_pickle_by_value(mod)
        except Exception:
            pass
        _user_modules_registered.add(name)


class _ScanPickler(cloudpickle.Pickler):
    """cloudpickle, noting the module of every non-builtin object as it goes by.

    That is the set of modules loading the pickle will import. Collecting it
    here costs nothing for plain data (ints, strings, lists never reach
    reducer_override), where walking the finished pickle opcode by opcode
    took seconds on large pure-Python payloads.
    """

    def __init__(self, file, mods: set, **kw):
        super().__init__(file, **kw)
        self._mods = mods

    def reducer_override(self, obj):
        m = type(obj).__module__
        if m != "builtins":
            self._mods.add(m.partition(".")[0])
        if isinstance(obj, types.ModuleType):
            self._mods.add(obj.__name__.partition(".")[0])
        else:
            m = getattr(obj, "__module__", None)
            if isinstance(m, str):
                self._mods.add(m.partition(".")[0])
        return super().reducer_override(obj)


def dumps(obj, memo: HashMemo | None = None, scan: bool = True) -> Packed:
    bufs: list[memoryview] = []

    def cb(pb: pickle.PickleBuffer):
        view = as_bytes_view(pb)
        if view.nbytes < OOB_MIN:
            return True
        bufs.append(view)
        return False

    mods: set[str] = set()
    if scan:
        f = io.BytesIO()
        _ScanPickler(f, mods, protocol=5, buffer_callback=cb).dump(obj)
        data = f.getvalue()
        mods |= code_imports(obj)
        mods.discard("")
    else:
        data = cloudpickle.dumps(obj, protocol=5, buffer_callback=cb)
    hashes = [memo.get(b) if memo else digest(b) for b in bufs]
    return Packed(data, bufs, hashes, mods)


def loads(data, buffers=()):
    return pickle.loads(data, buffers=buffers)


def dumps_plain(obj) -> Packed:
    """Results: out-of-band buffers but no hashing (they are rarely re-sent)."""
    bufs: list[memoryview] = []

    def cb(pb):
        view = as_bytes_view(pb)
        if view.nbytes < OOB_MIN:
            return True
        bufs.append(view)
        return False

    return Packed(cloudpickle.dumps(obj, protocol=5, buffer_callback=cb), bufs)


# ------------------------------------------------------------ module discovery

def pickle_modules(data: bytes) -> set[str]:
    """Top-level modules a pickle will import when loaded.

    Follows GLOBAL/STACK_GLOBAL opcodes, tracking the memo so STACK_GLOBAL's two
    string operands can be recovered even when they were memoized earlier.
    """
    mods: set[str] = set()
    memo: dict[int, object] = {}
    strs: list[object] = []  # recent pushes, enough to resolve STACK_GLOBAL
    try:
        for op, arg, _ in pickletools.genops(io.BytesIO(data)):
            name = op.name
            if name in ("SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8", "UNICODE"):
                strs.append(arg)
            elif name == "MEMOIZE":
                memo[len(memo)] = strs[-1] if strs else None
            elif name in ("BINPUT", "LONG_BINPUT", "PUT"):
                memo[arg] = strs[-1] if strs else None
            elif name in ("BINGET", "LONG_BINGET", "GET"):
                strs.append(memo.get(arg))
            elif name == "STACK_GLOBAL":
                if len(strs) >= 2 and isinstance(strs[-2], str):
                    mods.add(strs[-2].split(".")[0])
                strs.append(None)
            elif name in ("GLOBAL", "INST"):
                mods.add(arg.split(" ")[0].split(".")[0])
                strs.append(None)
            else:
                strs.append(None)
            if len(strs) > 64:
                del strs[:-8]
    except Exception:
        pass
    mods.discard("")
    return mods


def code_imports(obj, _seen=None) -> set[str]:
    """Modules imported inside function bodies (not visible in the pickle)."""
    seen = _seen if _seen is not None else set()
    out: set[str] = set()
    stack = [obj]
    while stack:
        o = stack.pop()
        if id(o) in seen:
            continue
        seen.add(id(o))
        if isinstance(o, types.FunctionType):
            stack.append(o.__code__)
            for c in o.__closure__ or ():
                try:
                    stack.append(c.cell_contents)
                except ValueError:
                    pass
        elif isinstance(o, types.CodeType):
            for ins in dis.get_instructions(o):
                if ins.opname == "IMPORT_NAME" and isinstance(ins.argval, str):
                    out.add(ins.argval.split(".")[0])
            stack.extend(c for c in o.co_consts if isinstance(c, types.CodeType))
        elif hasattr(o, "func") and hasattr(o, "args"):  # functools.partial
            stack.append(o.func)
            stack.extend(o.args)
        elif isinstance(o, (list, tuple)) and len(o) < 64:
            stack.extend(o)
        elif isinstance(o, types.MethodType):
            stack.append(o.__func__)
    out.discard("")
    return out
