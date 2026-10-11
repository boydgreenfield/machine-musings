"""Function regions of a Python source file, for the neutral bundle's ``functions.jsonl``.

This is the control plane's rule set (``coverset.analysis.functions.index_functions``) reproduced
without depending on coverset, and pinned against it field by field by
``tests/plugin_conformance/test_regions_match.py`` in the coverset workspace:

* every ``def``/``async def``/``class`` reachable through :func:`ast.iter_child_nodes` is a region
  (so definitions inside ``if``/``try``/``with``/``match`` are found; lambdas and comprehensions are
  not), plus one ``module`` region named ``<module>``;
* ``qualname`` follows CPython's ``co_qualname`` (``Cls.method``, ``outer.<locals>.inner``);
  ``ordinal`` numbers same-qualname definitions in source order; ``func_key`` is
  ``blake2b(path\\0qualname\\0ordinal, digest_size=8)`` in hex;
* ``def_line`` is the first decorator line, else the ``def`` line (``co_firstlineno``);
  ``body_start`` the first statement after a leading docstring (``def_line`` for a function whose
  body is only a docstring, the docstring line for such a class); ``end_line`` the node's last line;
* ``body_hash`` is blake2b-8 of the AST dump of the body without its docstring (nested
  definitions replaced by ``pass`` for classes and the module); ``sig_hash`` digests the
  arguments, decorators, return annotation and type parameters (bases and keywords for a class);
* a file that does not parse yields only its module region, hashed over the raw text.

Declared parameter types (``types`` in ``functions.jsonl``) are not written: for Python the control
plane derives them from the sources itself, and ``coverset bundle convert --source-root`` adds them.
"""

from __future__ import annotations

import ast
import copy
import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = ["MODULE_QUALNAME", "Region", "func_key", "index_functions"]

MODULE_QUALNAME: Final = "<module>"

type DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
type RegionKind = Literal["function", "method", "class", "module"]

_DEF_TYPES: Final = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_HASH_BYTES: Final = 8


@dataclass(frozen=True, slots=True)
class Region:
    """One function, method, class or module region (a ``functions.jsonl`` row)."""

    func_key: str
    path: str
    qualname: str
    ordinal: int
    def_line: int
    body_start: int
    end_line: int
    body_hash: str
    sig_hash: str
    params: tuple[str, ...]
    kind: RegionKind

    def as_row(self) -> dict[str, Any]:
        """The ``functions.jsonl`` row (``key`` is ``func_key``; no ``types``)."""
        return {
            "key": self.func_key,
            "path": self.path,
            "qualname": self.qualname,
            "ordinal": self.ordinal,
            "def_line": self.def_line,
            "body_start": self.body_start,
            "end_line": self.end_line,
            "body_hash": self.body_hash,
            "sig_hash": self.sig_hash,
            "params": list(self.params),
            "kind": self.kind,
        }


def func_key(path: str, qualname: str, ordinal: int) -> str:
    """The stable 16-hex-digit key of a region: ``blake2b(path\\0qualname\\0ordinal, 8)``."""
    return hashlib.blake2b(f"{path}\0{qualname}\0{ordinal}".encode(), digest_size=8).hexdigest()


def index_functions(path: str, source: str) -> list[Region]:
    """Every module, class, function and method region of ``source`` (module first, then source order)."""
    n_lines = max(1, len(source.splitlines()))
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return [_region(path, MODULE_QUALNAME, 0, 1, 1, n_lines, _digest(source), _digest(""), (), "module")]
    found = [
        _region(
            path,
            MODULE_QUALNAME,
            0,
            1,
            _body_start(tree.body, default=1),
            n_lines,
            _digest(_dump_body(tree.body, skeleton=True)),
            _digest(""),
            (),
            "module",
        )
    ]
    ordinals: dict[str, int] = {}
    for node, qualname, in_class in _walk_defs(tree, prefix="", in_class=False):
        ordinal = ordinals.get(qualname, 0)
        ordinals[qualname] = ordinal + 1
        found.append(_index_def(path, node, qualname, ordinal, in_class))
    return found


def _walk_defs(node: ast.AST, *, prefix: str, in_class: bool) -> Iterator[tuple[DefNode, str, bool]]:
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


def _index_def(path: str, node: DefNode, qualname: str, ordinal: int, in_class: bool) -> Region:
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
        return _region(
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
    rest = _strip_docstring(node.body)
    body_start = _first_line(rest[0]) if rest else def_line
    sig_parts = [
        type(node).__name__,
        ast.dump(node.args, annotate_fields=False),
        *(ast.dump(d, annotate_fields=False) for d in node.decorator_list),
        ast.dump(node.returns, annotate_fields=False) if node.returns else "",
        *(ast.dump(t, annotate_fields=False) for t in node.type_params),
    ]
    return _region(
        path,
        qualname,
        ordinal,
        def_line,
        body_start,
        end_line,
        _digest(_dump_body(node.body, skeleton=False)),
        _digest("\n".join(sig_parts)),
        _params(node.args),
        "method" if in_class else "function",
    )


def _params(args: ast.arguments) -> tuple[str, ...]:
    """Parameter names in signature order; ``*args`` and ``**kwargs`` keep their stars."""
    names = [a.arg for a in args.posonlyargs] + [a.arg for a in args.args]
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    names += [a.arg for a in args.kwonlyargs]
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return tuple(names)


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
    rest = _strip_docstring(body)
    if rest:
        return _first_line(rest[0])
    return body[0].lineno if body else default


def _first_line(stmt: ast.stmt) -> int:
    if isinstance(stmt, _DEF_TYPES) and stmt.decorator_list:
        return min(stmt.lineno, *(d.lineno for d in stmt.decorator_list))
    return stmt.lineno


def _dump_body(body: Sequence[ast.stmt], *, skeleton: bool) -> str:
    stmts = _strip_docstring(body)
    if skeleton:
        stmts = [_skeleton(stmt) for stmt in stmts]
    return ",".join(ast.dump(stmt, annotate_fields=False) for stmt in stmts)


def _skeleton(stmt: ast.stmt) -> ast.stmt:
    if isinstance(stmt, _DEF_TYPES):
        return ast.Pass()
    if not any(isinstance(node, _DEF_TYPES) for node in ast.walk(stmt)):
        return stmt
    return _SkeletonTransformer().visit(copy.deepcopy(stmt))


class _SkeletonTransformer(ast.NodeTransformer):
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
) -> Region:
    return Region(
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
