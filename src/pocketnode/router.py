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


def release(v: str) -> str:
    """major.minor.patch, without pre/post/dev/local parts: "2.5.2.post1" -> "2.5.2"."""
    try:
        from packaging.version import Version
        parts = list(Version(v).release)
    except Exception:  # noqa: BLE001 - not a PEP 440 version: take its leading numbers
        import re
        m = re.match(r"\d+(?:\.\d+)*", v)
        parts = [int(x) for x in m.group(0).split(".")] if m else [v]
    parts = (parts + [0, 0, 0])[:3]
    return ".".join(map(str, parts))


def policy_for(node, version_policy: str | None) -> str:
    """The version policy for one node: explicit, else "release" for a fixed package set, else "minor"."""
    if version_policy:
        return version_policy
    return "release" if node.hello.get("fixed_packages") else "minor"


def same_version(have: str, want: str, policy: str) -> bool:
    if policy == "any":
        return True
    if policy == "exact":
        return have == want
    if policy == "release":
        return release(have) == release(want)
    return _minor(have) == _minor(want)


def check(modules: set[str], node, version_policy: str | None = None) -> Verdict:
    """Static check of a task's referenced modules against a node's capabilities.

    version_policy: "minor", "release", "exact" or "any"; None picks per node
    ("release" where the package set is fixed, as in the App Store edition)."""
    if node.kind == "mac" and node.same_env:
        return Verdict(True)
    hello = node.hello
    fixed = bool(hello.get("fixed_packages"))
    policy = policy_for(node, version_policy)
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
                suggestion=None if fixed else
                f"add {info['dist']} to the phone with `pocketnode packages add {info['dist']}`",
            )
        if not same_version(have, info["version"], policy):
            if fixed:
                tip = (f"install {info['dist']}=={release(have)} on the Mac to use {node.name} for this "
                       "(its package set is fixed)")
            else:
                tip = (f"match versions (`pocketnode packages add {info['dist']}=={info['version']}`) "
                       "or allow it with version_policy='any'")
            return Verdict(
                False,
                f"{info['dist']} is {have} on {node.name} but {info['version']} on the Mac",
                missing=info["dist"],
                suggestion=tip,
            )
    return Verdict(True)


def is_pure(modules: set[str]) -> bool:
    """Only the stdlib and pure-Python packages that don't build on compiled ones
    (so the task can run in a subinterpreter, where extension modules can't load)."""
    import sys
    man = envinfo.manifest()
    for m in modules - IGNORE:
        if m in sys.stdlib_module_names:
            continue
        info = man.get(m)
        if info is not None and (info["kind"] == "compiled" or envinfo.needs_native(info["dist"])):
            return False
    return True
