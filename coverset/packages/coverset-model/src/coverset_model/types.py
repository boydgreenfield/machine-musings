"""Type descriptors: the language-neutral declared type of a parameter (``docs/neutral-bundle.md``).

A descriptor is a small tree of :class:`TypeDescriptor` nodes, one ``kind`` per node:

========================================  =========================================================
``int``, ``float``, ``str``, ``bool``,    a scalar type
``bytes``, ``none``
``list``, ``set``, ``frozenset``          a collection; ``elem`` is the element type (omitted when
                                          the declaration names no element type, e.g. bare ``list``)
``tuple``                                 ``elems`` for a fixed-size tuple, ``elem`` for a
                                          variable-length one (``tuple[int, ...]``), neither for
                                          bare ``tuple``
``dict``                                  ``key`` and ``value`` (each may be omitted)
``union``                                 ``arms``; ``Optional[X]`` is a union with ``none``
``literal``                               ``values``: JSON scalars (str, int, float, bool, null)
``enum``                                  ``name`` and ``members``
``object``                                ``name`` of a class, interface or anything else
``any``                                   unknown
========================================  =========================================================

:func:`expected_families` reduces a descriptor to the facts the input-class catalogue reads
(:class:`Expectations`), so the catalogue never looks at a language's own type syntax.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "ANY",
    "COLLECTION_KINDS",
    "KINDS",
    "SCALAR_KINDS",
    "DescriptorError",
    "Expectations",
    "TypeDescriptor",
    "expected_families",
    "flatten_union",
    "from_json",
    "label",
    "to_json",
]

type Kind = Literal[
    "int",
    "float",
    "str",
    "bool",
    "bytes",
    "none",
    "list",
    "set",
    "frozenset",
    "tuple",
    "dict",
    "union",
    "literal",
    "enum",
    "object",
    "any",
]
type LiteralValue = str | int | float | bool | None

SCALAR_KINDS: Final = frozenset({"int", "float", "str", "bool", "bytes", "none"})
COLLECTION_KINDS: Final = frozenset({"list", "set", "frozenset", "tuple"})
KINDS: Final = (
    SCALAR_KINDS | COLLECTION_KINDS | frozenset({"dict", "union", "literal", "enum", "object", "any"})
)
MAX_DEPTH: Final = 32
MAX_ITEMS: Final = 256
MAX_NAME_CHARS: Final = 512
MAX_STRING_VALUE_CHARS: Final = 512

_NUMERIC: Final = frozenset({"int", "float"})
_COMPARABLE: Final = frozenset({"int", "float", "str", "bytes", "tuple"})
_ORDERED: Final = frozenset({"list", "tuple"})


class DescriptorError(ValueError):
    """A JSON value is not a type descriptor."""


@dataclass(frozen=True, slots=True)
class TypeDescriptor:
    """One node of a type descriptor tree (see the module docstring for the fields of each kind)."""

    kind: Kind
    elem: TypeDescriptor | None = None
    elems: tuple[TypeDescriptor, ...] | None = None
    key: TypeDescriptor | None = None
    value: TypeDescriptor | None = None
    arms: tuple[TypeDescriptor, ...] = ()
    values: tuple[LiteralValue, ...] = ()
    name: str | None = None
    members: tuple[str, ...] = ()

    # --- constructors ---------------------------------------------------------------------

    @classmethod
    def scalar(cls, kind: Kind) -> TypeDescriptor:
        """``int``, ``float``, ``str``, ``bool``, ``bytes`` or ``none``."""
        if kind not in SCALAR_KINDS:
            raise DescriptorError(f"{kind!r} is not a scalar kind")
        return cls(kind)

    @classmethod
    def list_of(cls, elem: TypeDescriptor | None = None) -> TypeDescriptor:
        return cls("list", elem=elem)

    @classmethod
    def set_of(cls, elem: TypeDescriptor | None = None, *, frozen: bool = False) -> TypeDescriptor:
        return cls("frozenset" if frozen else "set", elem=elem)

    @classmethod
    def tuple_of(
        cls, elems: Sequence[TypeDescriptor] | None = None, *, elem: TypeDescriptor | None = None
    ) -> TypeDescriptor:
        """A fixed tuple (``elems``), a variable-length one (``elem``) or a bare tuple (neither)."""
        if elems is not None and elem is not None:
            raise DescriptorError("a tuple has either elems or elem, not both")
        return cls("tuple", elem=elem, elems=tuple(elems) if elems is not None else None)

    @classmethod
    def dict_of(
        cls, key: TypeDescriptor | None = None, value: TypeDescriptor | None = None
    ) -> TypeDescriptor:
        return cls("dict", key=key, value=value)

    @classmethod
    def union(cls, *arms: TypeDescriptor) -> TypeDescriptor:
        """A union of ``arms`` (nested unions are flattened, a single arm is returned as is)."""
        flat = tuple(a for arm in arms for a in flatten_union(arm))
        if not flat:
            raise DescriptorError("a union needs at least one arm")
        return flat[0] if len(flat) == 1 else cls("union", arms=flat)

    @classmethod
    def literal(cls, *values: LiteralValue) -> TypeDescriptor:
        return cls("literal", values=tuple(values))

    @classmethod
    def enum(cls, name: str, members: Sequence[str]) -> TypeDescriptor:
        return cls("enum", name=name, members=tuple(members))

    @classmethod
    def object(cls, name: str) -> TypeDescriptor:
        return cls("object", name=name)

    # --- JSON -------------------------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """The descriptor as the JSON object of ``functions.jsonl``'s ``types``."""
        return to_json(self)


ANY: Final = TypeDescriptor("any")


def to_json(descriptor: TypeDescriptor) -> dict[str, Any]:
    """The JSON object for ``descriptor`` (only the fields its kind uses)."""
    d = descriptor
    out: dict[str, Any] = {"kind": d.kind}
    if d.kind in ("list", "set", "frozenset", "tuple") and d.elem is not None:
        out["elem"] = to_json(d.elem)
    if d.kind == "tuple" and d.elems is not None:
        out["elems"] = [to_json(e) for e in d.elems]
    if d.kind == "dict":
        if d.key is not None:
            out["key"] = to_json(d.key)
        if d.value is not None:
            out["value"] = to_json(d.value)
    if d.kind == "union":
        out["arms"] = [to_json(a) for a in d.arms]
    if d.kind == "literal":
        out["values"] = list(d.values)
    if d.kind in ("enum", "object"):
        out["name"] = d.name
    if d.kind == "enum":
        out["members"] = list(d.members)
    return out


def from_json(value: object, *, _depth: int = 0) -> TypeDescriptor:
    """Parse a descriptor from untrusted JSON; raises :class:`DescriptorError` on anything else.

    Bounded: at most :data:`MAX_DEPTH` levels, :data:`MAX_ITEMS` arms, elements, values or members
    per node, names of at most :data:`MAX_NAME_CHARS` characters. Unknown fields are refused.
    """
    if _depth > MAX_DEPTH:
        raise DescriptorError(f"descriptor nested deeper than {MAX_DEPTH} levels")
    if not isinstance(value, dict):
        raise DescriptorError("a descriptor is a JSON object")
    kind = value.get("kind")
    if kind not in KINDS:
        raise DescriptorError(f"unknown descriptor kind {kind!r}"[:200])
    allowed = {"kind", *_FIELDS[kind]}
    extra = sorted(set(value) - allowed)
    if extra:
        raise DescriptorError(f"{kind} descriptor has unknown fields: {', '.join(map(str, extra))}"[:200])

    def sub(item: object) -> TypeDescriptor:
        return from_json(item, _depth=_depth + 1)

    def opt(name: str) -> TypeDescriptor | None:
        return sub(value[name]) if value.get(name) is not None else None

    def items(name: str) -> list[Any]:
        found = value.get(name)
        if not isinstance(found, list):
            raise DescriptorError(f"{kind} descriptor needs a list {name!r}")
        if len(found) > MAX_ITEMS:
            raise DescriptorError(f"{kind} descriptor has more than {MAX_ITEMS} {name}")
        return found

    if kind in SCALAR_KINDS or kind == "any":
        return TypeDescriptor(kind)
    if kind in ("list", "set", "frozenset"):
        return TypeDescriptor(kind, elem=opt("elem"))
    if kind == "tuple":
        if value.get("elems") is not None and value.get("elem") is not None:
            raise DescriptorError("a tuple has either elems or elem, not both")
        elems = tuple(sub(e) for e in items("elems")) if value.get("elems") is not None else None
        return TypeDescriptor("tuple", elem=opt("elem"), elems=elems)
    if kind == "dict":
        return TypeDescriptor("dict", key=opt("key"), value=opt("value"))
    if kind == "union":
        arms = tuple(sub(a) for a in items("arms"))
        if not arms:
            raise DescriptorError("a union needs at least one arm")
        return TypeDescriptor("union", arms=arms)
    if kind == "literal":
        values = items("values")
        for v in values:
            if not (v is None or isinstance(v, str | int | float | bool)):
                raise DescriptorError("literal values are JSON scalars")
            if isinstance(v, str) and len(v) > MAX_STRING_VALUE_CHARS:
                raise DescriptorError(f"literal value longer than {MAX_STRING_VALUE_CHARS} characters")
        return TypeDescriptor("literal", values=tuple(values))
    name = value.get("name")
    if not isinstance(name, str) or not 0 < len(name) <= MAX_NAME_CHARS:
        raise DescriptorError(f"{kind} descriptor needs a name of 1 to {MAX_NAME_CHARS} characters")
    if kind == "object":
        return TypeDescriptor("object", name=name)
    members = items("members")
    if not all(isinstance(m, str) and 0 < len(m) <= MAX_NAME_CHARS for m in members):
        raise DescriptorError("enum members are non-empty strings")
    return TypeDescriptor("enum", name=name, members=tuple(members))


_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    **{k: () for k in SCALAR_KINDS},
    "any": (),
    "list": ("elem",),
    "set": ("elem",),
    "frozenset": ("elem",),
    "tuple": ("elem", "elems"),
    "dict": ("key", "value"),
    "union": ("arms",),
    "literal": ("values",),
    "enum": ("name", "members"),
    "object": ("name",),
}


def flatten_union(descriptor: TypeDescriptor) -> tuple[TypeDescriptor, ...]:
    """The arms of a union (nested unions flattened), or ``(descriptor,)`` for anything else."""
    if descriptor.kind != "union":
        return (descriptor,)
    return tuple(a for arm in descriptor.arms for a in flatten_union(arm))


def label(descriptor: TypeDescriptor) -> str:
    """The name an arm gives its ``type`` class: the kind, or the short name of an enum or object.

    ``none`` for None, ``Color`` for ``{"kind": "enum", "name": "pkg.Color"}``, ``Money`` for an
    object ``ledger.util.money.Money`` (the last dotted part, as the shape key's ``obj:`` token is
    shown), ``Any`` for ``any``, ``Literal`` and ``Union`` for those kinds.
    """
    if descriptor.kind in ("enum", "object") and descriptor.name:
        return descriptor.name.rsplit(".", 1)[-1]
    return {"any": "Any", "literal": "Literal", "union": "Union"}.get(descriptor.kind, descriptor.kind)


@dataclass(frozen=True, slots=True)
class ArmClasses:
    """Classes one union arm adds by itself: its ``Literal`` values or its ``Enum`` members."""

    family: Literal["literal", "enum"]
    name: str
    """The enum's short name (``Color``); empty for a literal arm."""
    classes: tuple[str, ...]
    """``repr`` of each literal value, or each member as ``Name.MEMBER``."""


@dataclass(frozen=True, slots=True)
class Expectations:
    """What a declared type contributes to a parameter's expected input classes.

    Data-driven families (numbers, strings, containers) open only when a value of that kind was
    observed; the catalogue combines these facts with the observations
    (``coverset_model.catalogue``). ``docs/input-classes.md`` has the table.
    """

    arms: tuple[TypeDescriptor, ...]
    """The top-level union arms (one arm for a non-union)."""
    type_classes: tuple[str, ...]
    """One ``type`` class per non-literal arm, when there are two or more arms."""
    none: bool
    """An arm is ``none`` (``Optional``)."""
    bool: bool
    """An arm is ``bool``: both ``true`` and ``false`` are expected."""
    float: bool
    """An arm is ``float``: ``nan`` and ``inf`` join the number family once it is open."""
    elem_names: frozenset[str]
    """Labels of the element types of collection arms (union elements flattened)."""
    ordered: bool
    """An arm is a ``list`` or ``tuple``."""
    numeric_pair: bool
    """An element type is a fixed 2-tuple of numbers (``list[tuple[int, int]]``)."""
    arm_classes: tuple[ArmClasses, ...]
    """Literal values and enum members, in arm order."""

    @property
    def numeric_elements(self) -> bool:
        """The element type is numeric (every element arm is ``int`` or ``float``)."""
        return bool(self.elem_names) and self.elem_names <= _NUMERIC

    @property
    def comparable_elements(self) -> bool:
        """The element type compares with builtin semantics (numbers, strings, bytes, tuples)."""
        return bool(self.elem_names) and self.elem_names <= _COMPARABLE

    @property
    def float_elements(self) -> bool:
        return "float" in self.elem_names


def _element_arms(arm: TypeDescriptor) -> tuple[TypeDescriptor, ...]:
    """Element types of a collection arm; a fixed tuple contributes none."""
    if arm.kind in ("list", "set", "frozenset") or (arm.kind == "tuple" and arm.elems is None):
        return flatten_union(arm.elem) if arm.elem is not None else ()
    return ()


def _is_numeric_pair(arm: TypeDescriptor) -> bool:
    return (
        arm.kind == "tuple"
        and arm.elems is not None
        and len(arm.elems) == 2
        and all(label(e) in _NUMERIC for e in arm.elems)
    )


def expected_families(descriptor: TypeDescriptor | None) -> Expectations:
    """Reduce a descriptor (None: unannotated) to the :class:`Expectations` the catalogue applies."""
    arms = flatten_union(descriptor) if descriptor is not None else ()
    elem_arms = [e for arm in arms for e in _element_arms(arm)]
    arm_classes: list[ArmClasses] = []
    for arm in arms:
        if arm.kind == "literal":
            arm_classes.append(ArmClasses("literal", "", tuple(repr(v) for v in arm.values)))
        elif arm.kind == "enum" and arm.members:
            short = label(arm)
            arm_classes.append(ArmClasses("enum", short, tuple(f"{short}.{m}" for m in arm.members)))
    return Expectations(
        arms=arms,
        type_classes=tuple(label(a) for a in arms if a.kind != "literal") if len(arms) > 1 else (),
        none=any(a.kind == "none" for a in arms),
        bool=any(a.kind == "bool" for a in arms),
        float=any(a.kind == "float" for a in arms),
        elem_names=frozenset(label(e) for e in elem_arms),
        ordered=any(a.kind in _ORDERED for a in arms),
        numeric_pair=any(_is_numeric_pair(e) for e in elem_arms),
        arm_classes=tuple(arm_classes),
    )
