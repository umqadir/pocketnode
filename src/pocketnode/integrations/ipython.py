"""Jupyter cell magics: run a cell on the phone (or wherever it fits best).

    %load_ext pocketnode

    %%phone                # run this cell on the phone
    model = fit(X, y)      # X, y are shipped; model comes back into the notebook

    %%pocket               # same, but placement is automatic (Mac or phone)

The cell's free variables are sent, the names it assigns come back, its output
is printed here, and the last expression is displayed as usual.
"""

from __future__ import annotations

import ast
import builtins
import types

from IPython.core.magic import Magics, cell_magic, magics_class
from IPython.core.magic_arguments import argument, magic_arguments, parse_argstring


def _names(cell: str) -> tuple[set[str], set[str], bool]:
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


def _run_cell(cell: str, inputs: dict, outputs: list[str], ends_expr: bool):
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


@magics_class
class PocketMagics(Magics):
    def _run(self, line: str, cell: str, placement: str):
        from .. import current
        args = parse_argstring(self.phone, line)
        ns = self.shell.user_ns
        loads, stores, ends_expr = _names(cell)
        inputs = {k: ns[k] for k in loads if k in ns and not k.startswith("_") and not hasattr(builtins, k)}
        ex = current()
        where = args.node or placement
        fut = ex._submit(_run_cell, (cell, inputs, sorted(stores), ends_expr), {}, where, None)
        out, value = fut.result()
        ns.update(out)
        if args.verbose:
            print(fut.placement)
        return value

    @magic_arguments()
    @argument("--node", help="run on this node (name or kind) instead")
    @argument("-v", "--verbose", action="store_true", help="say where the cell ran and why")
    @cell_magic
    def phone(self, line, cell):
        return self._run(line, cell, "phone")

    @magic_arguments()
    @argument("--node", help="run on this node (name or kind)")
    @argument("-v", "--verbose", action="store_true", help="say where the cell ran and why")
    @cell_magic
    def pocket(self, line, cell):
        return self._run(line, cell, "auto")


def load_ipython_extension(ip):
    ip.register_magics(PocketMagics)
