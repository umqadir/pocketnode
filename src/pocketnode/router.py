"""Placement: can a node run a task, and if not, why (and what would fix it)."""

from __future__ import annotations

from dataclasses import dataclass

from . import envinfo

# The task imports these itself; on iOS they can only fail.
PROCESS_MODULES = {"subprocess", "multiprocessing", "pty"}
# Never matter for placement: present everywhere the worker runs.
IGNORE = {"__main__", "builtins", "cloudpickle", "copyreg", "_codecs", "pocketnode", "__builtin__"}


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    missing: str | None = None  # a compiled package that would unlock this node
    suggestion: str | None = None


def _norm(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def _minor(v: str) -> str:
    return ".".join(v.split(".")[:2])


def check(modules: set[str], node, version_policy: str = "minor") -> Verdict:
    """Static check of a task's referenced modules against a node's capabilities."""
    if node.kind == "mac" and node.same_env:
        return Verdict(True)
    hello = node.hello
    pkgs = getattr(node, "packages", None)
    if pkgs is None:
        pkgs = {_norm(k): v for k, v in hello.get("packages", {}).items()}
    man = envinfo.manifest()
    for m in sorted(modules - IGNORE):
        if m in node.mac_only_modules:
            return Verdict(False, node.mac_only_modules[m], missing=m)
        if m in PROCESS_MODULES:
            return Verdict(False, f"uses {m}, and iOS apps can't start processes")
        if m in hello.get("stdlib_missing", ()):
            return Verdict(False, f"uses {m}, which iOS's Python doesn't have")
        info = man.get(m)
        if info is None or info["kind"] == "pure":
            continue  # stdlib, the user's own code, or fetched from the Mac on demand
        have = pkgs.get(_norm(info["dist"]))
        if have is None:
            return Verdict(
                False,
                f"needs {info['dist']} (compiled), which isn't installed on {node.name}",
                missing=info["dist"],
                suggestion=f"add {info['dist']} to the phone with `pocketnode packages add {info['dist']}`",
            )
        if version_policy != "any":
            same = have == info["version"] if version_policy == "exact" else _minor(have) == _minor(info["version"])
            if not same:
                return Verdict(
                    False,
                    f"{info['dist']} is {have} on {node.name} but {info['version']} on the Mac",
                    missing=info["dist"],
                    suggestion=f"match versions (`pocketnode packages add {info['dist']}=={info['version']}`) "
                               "or allow it with version_policy='any'",
                )
    return Verdict(True)


def is_pure(modules: set[str]) -> bool:
    """Only the stdlib and pure-Python packages (so it can run in a subinterpreter)."""
    import sys
    man = envinfo.manifest()
    for m in modules - IGNORE:
        if m in sys.stdlib_module_names:
            continue
        info = man.get(m)
        if info is not None and info["kind"] == "compiled":
            return False
    return True
