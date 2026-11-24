"""Running a notebook cell as a task. Kept apart from the IPython magics, so the
task that runs on the phone needs only the stdlib, not IPython and its dependencies."""

from __future__ import annotations

import ast
import types


def names(cell: str) -> tuple[set[str], set[str], bool]:
    """(names read, names assigned at top level, ends with an expression)."""
    tree = ast.parse(cell)
    loads, stores = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (loads if isinstance(node.ctx, ast.Load) else stores).add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stores.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                stores.add((a.asname or a.name).split(".")[0])
    ends_expr = bool(tree.body) and isinstance(tree.body[-1], ast.Expr)
    return loads, stores, ends_expr


def run_cell(cell: str, inputs: dict, outputs: list[str], ends_expr: bool):
    ns = dict(inputs)
    ns.setdefault("__name__", "__main__")
    tree = ast.parse(cell)
    last = None
    if ends_expr:
        last = ast.Expression(tree.body.pop().value)
    exec(compile(tree, "<cell>", "exec"), ns)
    value = eval(compile(last, "<cell>", "eval"), ns) if last is not None else None
    out = {k: ns[k] for k in outputs if k in ns and not isinstance(ns[k], types.ModuleType)}
    return out, value
