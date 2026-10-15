"""The input-class catalogue for Python sources: the language-neutral model plus annotations.

The catalogue itself (expected and observed classes, notes, markdown, suggestions) is language
neutral and lives in ``coverset_model.catalogue``; it reads declared types as
:class:`~coverset_model.types.TypeDescriptor` trees. This module re-exports its public names
unchanged and adds what only Python needs:

* :func:`annotation_index`: the annotation text of every function in a set of sources, by function
  triple (what :class:`Catalogue`'s ``annotations`` table takes; the catalogue converts text to
  descriptors with ``coverset_model.python_types.descriptor``);
* :func:`type_index`: the same as descriptors;
* :func:`enum_index`: the ``Enum`` classes defined in the sources, read with the AST, used to turn
  an annotation naming one into an ``enum`` descriptor;
* :func:`function_catalogue`: the :class:`FunctionCatalogue` of one function from its own head
  Python source, which the verdict, the PR page's cards, the function page and ``coverset
  invariants scan`` share so they count the same classes. A function whose declared types were
  shipped as descriptors (any non-Python run) is served by :func:`function_catalogue_from_types`
  over the descriptors stored at ingest instead (``RunFacts.catalogue_for`` in the web layer).

See ``docs/input-classes.md`` for the rules.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Mapping, Sequence
from typing import Final

from coverset.analysis.functions import FunctionRegion, IndexedFunction, index_functions_detailed
from coverset.lang.base import FuncTriple
from coverset_model.catalogue import (
    NEVER_EXERCISED,
    NON_INPUT_PARAMS,
    Catalogue,
    FunctionCatalogue,
    FunctionInputCov,
    InputClass,
    ParamInfo,
    cap_hits,
    describe,
    example_sort_key,
    function_catalogue_from_types,
    function_input_cov,
    render_markdown,
    suggest_annotation,
)
from coverset_model.python_types import descriptor
from coverset_model.shape_keys import ParsedShape, ShapeKeyError, parse_shape_key
from coverset_model.types import TypeDescriptor

__all__ = [
    "NEVER_EXERCISED",
    "NON_INPUT_PARAMS",
    "Catalogue",
    "FunctionCatalogue",
    "FunctionInputCov",
    "InputClass",
    "ParamInfo",
    "ParsedShape",
    "ShapeKeyError",
    "annotation_index",
    "cap_hits",
    "describe",
    "enum_index",
    "example_sort_key",
    "function_catalogue",
    "function_catalogue_from_types",
    "function_input_cov",
    "parse_shape_key",
    "render_markdown",
    "suggest_annotation",
    "type_index",
]

_ENUM_BASES: Final = frozenset({"Enum", "IntEnum", "StrEnum"})


def function_catalogue(
    region: FunctionRegion, source: str | None, *, indexed: Sequence[IndexedFunction] | None = None
) -> FunctionCatalogue:
    """The catalogue for one function, built the same way wherever its classes are shown.

    The verdict (the changed-function rows and the sticky comment's "N input classes never
    exercised"), the PR page's cards and the function page all call this for a Python function, so
    they count the same classes: the parameter annotations come from the function's own head ``source`` (matched by
    ``func_key``, else by qualname and ``def_line``) and become descriptors with the ``Enum``
    classes defined in that same file (:func:`enum_index`) resolved. ``indexed`` is
    ``index_functions_detailed(region.path, source)`` when the caller has it cached. The result
    keeps the annotation text for display (``annotations``, ``returns``) and the descriptors the
    classes are computed from (``types``).
    """
    if source is None:
        return FunctionCatalogue(Catalogue(), {}, None)
    if indexed is None:
        try:
            indexed = index_functions_detailed(region.path, source)
        except (SyntaxError, ValueError, RecursionError):
            indexed = []
    annotations: dict[str, str | None] = {}
    returns: str | None = None
    for item in indexed:
        if item.region.func_key == region.func_key or (
            item.region.qualname == region.qualname and item.region.def_line == region.def_line
        ):
            annotations = {_bare(p): a for p, a in zip(item.region.params, item.annotations, strict=True)}
            returns = item.returns
            break
    enums = _enums_of(region.path, source)
    resolver = enums.get if enums else None
    types: dict[str, TypeDescriptor | None] = {p: descriptor(a, resolver) for p, a in annotations.items()}
    types["return"] = descriptor(returns, resolver)
    return FunctionCatalogue(Catalogue(resolver), annotations, returns, types)


def annotation_index(sources: Mapping[str, str]) -> dict[FuncTriple, dict[str, str | None]]:
    """Parameter annotation text of every function in ``sources`` (path → text), by function triple.

    Feed it the head commit's measured source files and pass the result to :class:`Catalogue`
    (with :func:`enum_index`'s ``get`` as ``enum_members``) so ingest applies the annotation rules;
    the catalogue converts the text to descriptors. Keys of the inner mapping are bare parameter
    names. ``coverset shapes --suggest`` shows this text next to the suggested annotation.
    """
    index: dict[FuncTriple, dict[str, str | None]] = {}
    for path, source in sources.items():
        for item in index_functions_detailed(path, source):
            region = item.region
            if region.kind in ("function", "method"):
                index[region.triple] = {
                    _bare(p): a for p, a in zip(region.params, item.annotations, strict=True)
                }
    return index


def type_index(sources: Mapping[str, str]) -> dict[FuncTriple, dict[str, TypeDescriptor | None]]:
    """:func:`annotation_index` as descriptors (``return`` included), enums resolved over ``sources``.

    This is what a Python bundle converted to the neutral format ships as ``functions.jsonl``'s
    ``types`` (``coverset bundle convert --source-root``).
    """
    enums = enum_index(sources)
    resolver = enums.get if enums else None
    index: dict[FuncTriple, dict[str, TypeDescriptor | None]] = {}
    for path, source in sources.items():
        for item in index_functions_detailed(path, source):
            region = item.region
            if region.kind in ("function", "method"):
                found = {
                    _bare(p): descriptor(a, resolver)
                    for p, a in zip(region.params, item.annotations, strict=True)
                }
                found["return"] = descriptor(item.returns, resolver)
                index[region.triple] = found
    return index


@functools.lru_cache(maxsize=256)
def _enums_of(path: str, source: str) -> dict[str, tuple[str, ...]]:
    """:func:`enum_index` of one file, remembered per (path, text): a report builds a catalogue per
    function and would otherwise parse the whole file once per function."""
    return enum_index({path: source})


def enum_index(sources: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """Member names of every ``Enum``/``IntEnum``/``StrEnum`` class defined in ``sources``, by class name.

    Found with the AST only (nothing is imported): a class whose base is one of those names, or a
    class in ``sources`` already found to be an enum, contributes the names assigned in its body
    (``RED = 1``, ``RED: int = 1``), private names and ``_ignore_`` excluded. A class name defined
    twice with different members is left out (the annotation alone cannot say which one is meant).
    Pass the result's ``get`` as :class:`Catalogue`'s ``enum_members``.
    """
    classes: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for path in sorted(sources):
        try:
            tree = ast.parse(sources[path])
        except (SyntaxError, ValueError, RecursionError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = tuple(_name(b) for b in node.bases)
                classes.append((node.name, bases, _enum_member_names(node)))
    enums: set[str] = set()
    changed = True
    while changed:  # a subclass of an enum found in the sources is an enum too
        changed = False
        for name, bases, _ in classes:
            if name not in enums and any(b in _ENUM_BASES or b in enums for b in bases):
                enums.add(name)
                changed = True
    found: dict[str, tuple[str, ...]] = {}
    ambiguous: set[str] = set()
    for name, _, members in classes:
        if name not in enums or not members:
            continue
        if name in found and found[name] != members:
            ambiguous.add(name)
        found.setdefault(name, members)
    return {name: members for name, members in found.items() if name not in ambiguous}


def _enum_member_names(node: ast.ClassDef) -> tuple[str, ...]:
    names: list[str] = []
    for item in node.body:
        targets: list[ast.expr] = []
        if isinstance(item, ast.Assign):
            targets = list(item.targets)
        elif isinstance(item, ast.AnnAssign) and item.value is not None:
            targets = [item.target]
        for target in targets:
            if isinstance(target, ast.Name) and not target.id.startswith("_") and target.id not in names:
                names.append(target.id)
    return tuple(names)


def _bare(param: str) -> str:
    return param.lstrip("*")


def _name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ast.unparse(node)
