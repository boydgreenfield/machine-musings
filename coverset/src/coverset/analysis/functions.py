"""AST index of a Python file's function, class and module regions.

Every ``def``/``async def``/``class`` reachable through :func:`ast.iter_child_nodes` becomes a
:class:`~coverset.lang.base.FunctionRegion` (so definitions nested in ``if``/``try``/``with``/
``match`` blocks are found; lambdas and comprehensions are not regions). Qualnames follow CPython's
``co_qualname`` convention (``outer.<locals>.inner``, ``Cls.method``), ``def_line`` equals
``co_firstlineno`` (the first decorator line, else the ``def`` line) and same-qualname definitions
(``@overload`` stubs, property getter and setter, ``try``/``except`` redefinitions) are told apart
by ``ordinal``, their index in source order. One extra region of kind ``'module'`` (qualname
``<module>``) stands for the module-level statements.

``body_start`` is the first statement after the docstring. A function whose body is only a
docstring has no such statement (CPython does not even compile the docstring as one), and calling
it makes coverage.py record only ``co_firstlineno``, so its ``body_start`` is ``def_line``. The
same holds for one-line ``def f(): return x``. Callers that ask "did a test call F" must then look at
test contexts only, because import time executes the ``def`` line too.

Hashes are formatting-insensitive: ``body_hash`` is a blake2b digest of the AST dump of the body
without its docstring and ``sig_hash`` digests arguments, decorators, return annotation and type
parameters. For classes and the module the body dump replaces nested ``def``/``class`` statements
with a placeholder, so editing a method changes the method's hash but not its class's; a function's
hash covers its nested definitions, because they are part of what the function does when called.
"""

from __future__ import annotations

import ast
import copy
import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from coverset.lang.base import FunctionChange, FunctionRegion, func_key

__all__ = [
    "MODULE_QUALNAME",
    "FunctionChange",
    "FunctionRegion",
    "IndexedFunction",
    "index_functions",
    "index_functions_detailed",
    "own_lines",
]

MODULE_QUALNAME: Final = "<module>"
"""Qualname of the module region (CPython's ``co_qualname`` for module code)."""

type DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
type RegionKind = Literal["function", "method", "class", "module"]

_DEF_TYPES: Final = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_HASH_BYTES: Final = 8


@dataclass(frozen=True, slots=True)
class IndexedFunction:
    """A region plus the annotation text of its parameters, for the input-class catalogue."""

    region: FunctionRegion
    annotations: tuple[str | None, ...]
    """``ast.unparse`` of each parameter's annotation, aligned with ``region.params``."""
    returns: str | None


def index_functions(path: str, source: str) -> list[FunctionRegion]:
    """Return every function, method, class and module region of ``source`` in source order.

    A file that does not parse yields only its module region, hashed over the raw text, so a
    syntax error still registers as a module-level change.
    """
    return [item.region for item in index_functions_detailed(path, source)]


def index_functions_detailed(path: str, source: str) -> list[IndexedFunction]:
    """Like :func:`index_functions`, with each region's parameter annotations attached."""
    n_lines = max(1, len(source.splitlines()))
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        digest = _digest(source)
        region = _region(path, MODULE_QUALNAME, 0, 1, 1, n_lines, digest, _digest(""), (), "module")
        return [IndexedFunction(region, (), None)]
    ordinals: dict[str, int] = {}
    module = _region(
        path,
        MODULE_QUALNAME,
        0,
        def_line=1,
        body_start=_body_start(tree.body, default=1),
        end_line=n_lines,
        body_hash=_digest(_dump_body(tree.body, skeleton=True)),
        sig_hash=_digest(""),
        params=(),
        kind="module",
    )
    found = [IndexedFunction(module, (), None)]
    for node, qualname, in_class in _walk_defs(tree, prefix="", in_class=False):
        ordinal = ordinals.get(qualname, 0)
        ordinals[qualname] = ordinal + 1
        found.append(_index_def(path, node, qualname, ordinal, in_class))
    return found


def own_lines(region: FunctionRegion, regions: Sequence[FunctionRegion]) -> set[int]:
    """Lines that belong to ``region`` itself, for deciding whether a diff touched it.

    A function owns its whole ``[def_line, end_line]`` range (nested definitions included). A class
    or the module owns its range minus the ranges of the function and class regions inside it.
    """
    lines = set(range(region.def_line, region.end_line + 1))
    if region.kind in ("function", "method"):
        return lines
    for other in regions:
        if other is region or other.kind == "module":
            continue
        inside = region.def_line <= other.def_line and other.end_line <= region.end_line
        if inside and (region.kind == "module" or other.def_line > region.def_line):
            lines.difference_update(range(other.def_line, other.end_line + 1))
    return lines


def _walk_defs(node: ast.AST, *, prefix: str, in_class: bool) -> Iterator[tuple[DefNode, str, bool]]:
    """Yield ``(def node, qualname, defined directly in a class body)`` depth-first in source order."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _DEF_TYPES):
            qualname = f"{prefix}{child.name}"
            yield child, qualname, in_class
            if isinstance(child, ast.ClassDef):
                yield from _walk_defs(child, prefix=f"{qualname}.", in_class=True)
            else:
                yield from _walk_defs(child, prefix=f"{qualname}.<locals>.", in_class=False)
        elif not isinstance(child, ast.Lambda):
            yield from _walk_defs(child, prefix=prefix, in_class=in_class)


def _index_def(path: str, node: DefNode, qualname: str, ordinal: int, in_class: bool) -> IndexedFunction:
    def_line = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    end_line = node.end_lineno or node.lineno
    if isinstance(node, ast.ClassDef):
        sig_parts = [
            "class",
            *(ast.dump(b) for b in node.bases),
            *(ast.dump(k) for k in node.keywords),
            *(ast.dump(d) for d in node.decorator_list),
            *(ast.dump(t) for t in node.type_params),
        ]
        region = _region(
            path,
            qualname,
            ordinal,
            def_line,
            _body_start(node.body, default=node.lineno),
            end_line,
            _digest(_dump_body(node.body, skeleton=True)),
            _digest("\n".join(sig_parts)),
            (),
            "class",
        )
        return IndexedFunction(region, (), None)
    rest = _strip_docstring(node.body)
    body_start = _first_line(rest[0]) if rest else def_line  # docstring-only: the line a call records
    params, annotations = _params(node.args)
    sig_parts = [
        type(node).__name__,
        ast.dump(node.args, annotate_fields=False),
        *(ast.dump(d, annotate_fields=False) for d in node.decorator_list),
        ast.dump(node.returns, annotate_fields=False) if node.returns else "",
        *(ast.dump(t, annotate_fields=False) for t in node.type_params),
    ]
    region = _region(
        path,
        qualname,
        ordinal,
        def_line,
        body_start,
        end_line,
        _digest(_dump_body(node.body, skeleton=False)),
        _digest("\n".join(sig_parts)),
        params,
        "method" if in_class else "function",
    )
    returns = ast.unparse(node.returns) if node.returns else None
    return IndexedFunction(region, annotations, returns)


def _params(args: ast.arguments) -> tuple[tuple[str, ...], tuple[str | None, ...]]:
    """Parameter names in signature order (``*args``/``**kwargs`` keep their stars) and annotations."""
    ordered: list[tuple[str, ast.expr | None]] = [(a.arg, a.annotation) for a in args.posonlyargs]
    ordered += [(a.arg, a.annotation) for a in args.args]
    if args.vararg:
        ordered.append((f"*{args.vararg.arg}", args.vararg.annotation))
    ordered += [(a.arg, a.annotation) for a in args.kwonlyargs]
    if args.kwarg:
        ordered.append((f"**{args.kwarg.arg}", args.kwarg.annotation))
    names = tuple(name for name, _ in ordered)
    annotations = tuple(ast.unparse(ann) if ann is not None else None for _, ann in ordered)
    return names, annotations


def _strip_docstring(body: Sequence[ast.stmt]) -> Sequence[ast.stmt]:
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _body_start(body: Sequence[ast.stmt], *, default: int) -> int:
    """Line of the first body statement after a leading docstring (the docstring if it is alone)."""
    rest = _strip_docstring(body)
    if rest:
        return _first_line(rest[0])
    return body[0].lineno if body else default


def _first_line(stmt: ast.stmt) -> int:
    if isinstance(stmt, _DEF_TYPES) and stmt.decorator_list:
        return min(stmt.lineno, *(d.lineno for d in stmt.decorator_list))
    return stmt.lineno


def _dump_body(body: Sequence[ast.stmt], *, skeleton: bool) -> str:
    """Formatting-insensitive dump of ``body`` minus its docstring.

    With ``skeleton=True`` nested ``def``/``class`` statements are replaced by ``pass`` so they
    are hashed only by their own regions.
    """
    stmts = _strip_docstring(body)
    if skeleton:
        stmts = [_skeleton(stmt) for stmt in stmts]
    return ",".join(ast.dump(stmt, annotate_fields=False) for stmt in stmts)


def _skeleton(stmt: ast.stmt) -> ast.stmt:
    """``stmt`` with nested definitions replaced by ``pass`` (copied only when it holds one)."""
    if isinstance(stmt, _DEF_TYPES):
        return ast.Pass()
    if not any(isinstance(node, _DEF_TYPES) for node in ast.walk(stmt)):
        return stmt
    return _SkeletonTransformer().visit(copy.deepcopy(stmt))


class _SkeletonTransformer(ast.NodeTransformer):
    """Replace nested function and class definitions with ``pass``."""

    def _drop(self, node: ast.AST) -> ast.Pass:
        return ast.Pass()

    visit_FunctionDef = _drop
    visit_AsyncFunctionDef = _drop
    visit_ClassDef = _drop


def _region(
    path: str,
    qualname: str,
    ordinal: int,
    def_line: int,
    body_start: int,
    end_line: int,
    body_hash: str,
    sig_hash: str,
    params: tuple[str, ...],
    kind: RegionKind,
) -> FunctionRegion:
    return FunctionRegion(
        func_key=func_key(path, qualname, ordinal),
        path=path,
        qualname=qualname,
        ordinal=ordinal,
        def_line=def_line,
        body_start=body_start,
        end_line=end_line,
        body_hash=body_hash,
        sig_hash=sig_hash,
        params=params,
        kind=kind,
    )


def _digest(text: str) -> str:
    return hashlib.blake2b(text.encode(), digest_size=_HASH_BYTES).hexdigest()
