"""Python annotation text → :class:`~coverset_model.types.TypeDescriptor`, with :mod:`ast` only.

Nothing is imported or evaluated. The rules reproduce how coverset read annotations before
descriptors existed, so the input classes a Python function expects did not change:

* ``A | B``, ``Union[A, B]`` → a union (nested unions flattened); ``Optional[A]`` → ``A``'s arms
  plus ``none``; ``Annotated[A, ...]`` → ``A``; a string (``"list[int]"``) is parsed, then read
  the same way.
* ``int``, ``float``, ``str``, ``bool``, ``bytes`` and ``None`` → scalars.
* ``list``/``List``/``Sequence``/``MutableSequence`` → ``list``; ``tuple``/``Tuple`` → ``tuple``
  (``tuple[X, ...]`` variable-length, ``tuple[X, Y]`` fixed); ``set``/``Set``/``AbstractSet``/
  ``MutableSet``/``Iterable``/``Collection``/``Iterator`` → ``set`` (unordered); ``frozenset``/
  ``FrozenSet`` → ``frozenset``; ``dict``/``Dict``/``Mapping``/``MutableMapping`` → ``dict``.
* ``Literal[...]`` → the literal's constant values that are JSON scalars (a ``Literal`` with
  none left is an ``object`` named ``Literal``).
* ``Any`` → ``any``; a name the ``enums`` resolver knows → ``enum`` with its members; any other
  name (dotted names by their last part, subscripted names by their base) → ``object``.
* Text that does not parse → ``any``.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from coverset_model.types import ANY, TypeDescriptor

__all__ = ["EnumResolver", "descriptor"]

type EnumResolver = Mapping[str, Sequence[str]] | Callable[[str], Sequence[str] | None]
"""Enum class name → member names, as a mapping or a ``get``-like callable."""

MAX_ANNOTATION_CHARS: Final = 4096
_MAX_DEPTH: Final = 24
_SCALARS: Final = {"int": "int", "float": "float", "str": "str", "bool": "bool", "bytes": "bytes"}
_LISTS: Final = frozenset({"list", "List", "Sequence", "MutableSequence"})
_TUPLES: Final = frozenset({"tuple", "Tuple"})
_SETS: Final = frozenset({"set", "Set", "AbstractSet", "MutableSet", "Iterable", "Collection", "Iterator"})
_FROZENSETS: Final = frozenset({"frozenset", "FrozenSet"})
_DICTS: Final = frozenset({"dict", "Dict", "Mapping", "MutableMapping"})
_NOT_ENUMS: Final = frozenset(
    {*_SCALARS, *_LISTS, *_TUPLES, *_SETS, *_FROZENSETS, *_DICTS, "None", "Any", "Union", "Optional",
     "Literal", "Annotated"}
)  # fmt: skip
_NONE: Final = TypeDescriptor("none")


def descriptor(annotation_text: str | None, enums: EnumResolver | None = None) -> TypeDescriptor | None:
    """The descriptor of one Python annotation (``ast.unparse`` text); None for no annotation."""
    if annotation_text is None:
        return None
    if len(annotation_text) > MAX_ANNOTATION_CHARS:
        return ANY
    resolve = _resolver(enums)
    try:
        node = ast.parse(annotation_text, mode="eval").body
        return _Reader(resolve).read(node, 0)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return ANY


def _resolver(enums: EnumResolver | None) -> Callable[[str], Sequence[str] | None]:
    if enums is None:
        return lambda _name: None
    if isinstance(enums, Mapping):
        return enums.get
    return enums


class _Reader:
    def __init__(self, resolve: Callable[[str], Sequence[str] | None]) -> None:
        self.resolve = resolve

    def read(self, node: ast.expr, depth: int) -> TypeDescriptor:
        """A descriptor for ``node``; a union of its arms when it has several."""
        arms = self.arms(node, depth)
        return arms[0] if len(arms) == 1 else TypeDescriptor("union", arms=tuple(arms))

    def arms(self, node: ast.expr, depth: int) -> list[TypeDescriptor]:
        if depth > _MAX_DEPTH:
            return [ANY]
        nxt = depth + 1
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self.arms(node.left, nxt) + self.arms(node.right, nxt)
        if isinstance(node, ast.Subscript):
            base = _name(node.value)
            items = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
            if base == "Union":
                return [arm for item in items for arm in self.arms(item, nxt)]
            if base == "Optional":
                return [arm for item in items for arm in self.arms(item, nxt)] + [_NONE]
            if base == "Annotated" and items:
                return self.arms(items[0], nxt)
            if base == "Literal":
                values = tuple(
                    i.value for i in items if isinstance(i, ast.Constant) and _json_scalar(i.value)
                )
                return [
                    TypeDescriptor("literal", values=values) if values else TypeDescriptor.object("Literal")
                ]
            return [self.generic(base, items, nxt)]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                inner = ast.parse(node.value, mode="eval").body
            except (SyntaxError, ValueError):
                return [TypeDescriptor.object(node.value)] if node.value else [ANY]
            return self.arms(inner, nxt)
        return [self.named(_name(node), ())]

    def generic(self, base: str, items: list[ast.expr], depth: int) -> TypeDescriptor:
        args = [self.read(i, depth) for i in items]
        if base in _TUPLES:
            if len(items) == 2 and isinstance(items[1], ast.Constant) and items[1].value is Ellipsis:
                return TypeDescriptor.tuple_of(elem=args[0])
            return TypeDescriptor.tuple_of(args)
        return self.named(base, args)

    def named(self, name: str, args: Sequence[TypeDescriptor]) -> TypeDescriptor:
        first = args[0] if args else None
        if name == "None":
            return _NONE
        if name in _SCALARS:
            return TypeDescriptor(_SCALARS[name])  # type: ignore[arg-type]
        if name == "Any":
            return ANY
        if name in _LISTS:
            return TypeDescriptor.list_of(first)
        if name in _TUPLES:
            return TypeDescriptor.tuple_of(None)
        if name in _SETS:
            return TypeDescriptor.set_of(first)
        if name in _FROZENSETS:
            return TypeDescriptor.set_of(first, frozen=True)
        if name in _DICTS:
            return TypeDescriptor.dict_of(first, args[1] if len(args) > 1 else None)
        if name not in _NOT_ENUMS:
            members = self.resolve(name)
            if members:
                return TypeDescriptor.enum(name, tuple(members))
        return TypeDescriptor.object(name or "object")


def _json_scalar(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and node.value is None:
        return "None"
    if isinstance(node, ast.Constant) and node.value is Ellipsis:
        return "..."
    return ast.unparse(node)
