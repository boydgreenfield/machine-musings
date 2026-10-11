"""The shape-key grammar: ``<type>`` then ``|``-joined feature tokens, sorted (``docs/neutral-bundle.md``).

:func:`parse` splits a key into its type token, plain flags and ``prefix:value`` attributes and
accepts any well-formed key (the catalogue ignores tokens it does not know). :func:`validate`
checks a key against the version-1 vocabulary: the type token, which features each type may carry,
their values, canonical order and no duplicates. :func:`canonical` sorts and de-duplicates the
features of a key a producer built by hand.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = [
    "CONTAINER_TYPES",
    "FEATURES",
    "LEN_BUCKETS",
    "MAX_KEY_CHARS",
    "SCALAR_TYPES",
    "STRING_KINDS",
    "ParsedShape",
    "ShapeKeyError",
    "canonical",
    "is_valid",
    "parse",
    "parse_shape_key",
    "validate",
]

MAX_KEY_CHARS: Final = 512
SCALAR_TYPES: Final = ("NoneType", "bool", "int", "float", "str", "bytes")
CONTAINER_TYPES: Final = ("list", "tuple", "dict", "set", "frozenset")
LEN_BUCKETS: Final = ("0", "1", "2-10", "11-100", "101-1000", ">1000")
EMAIL_KINDS: Final = ("plain", "multi-label-domain", "plus-tag", "uppercase", "idn")
QUANTITY_KINDS: Final = (
    "single-unit",
    "multi-unit",
    "multi-letter-unit",
    "decimal",
    "signed",
    "trailing-garbage",
)
STRING_KINDS: Final = (
    *(f"email/{k}" for k in EMAIL_KINDS),
    *(f"quantity/{k}" for k in QUANTITY_KINDS),
    "numeric",
    "iso-date",
    "url",
)
ORDERS: Final = ("sorted", "reverse", "unsorted", "n/a")
PAIRS: Final = ("lt", "eq", "gt", "mixed")
_NUMERIC_SUMMARY: Final = ("any_zero", "any_neg", "any_nan", "any_inf", "all_zero", "sum_zero")
_CONTAINER_FLAGS: Final = frozenset({*_NUMERIC_SUMMARY, "dups"})
_CONTAINER_ATTRS: Final[Mapping[str, tuple[str, ...] | None]] = {
    "len": LEN_BUCKETS,
    "elem": None,  # a type token or "mixed"
    "order": ORDERS,
    "pair": PAIRS,
}

FEATURES: Final[Mapping[str, tuple[frozenset[str], Mapping[str, tuple[str, ...] | None]]]] = {
    "NoneType": (frozenset(), {}),
    "bool": (frozenset({"true", "false"}), {}),
    "int": (frozenset({"neg", "zero", "pos", "big"}), {}),
    "float": (frozenset({"nan", "inf", "inf-neg", "neg", "zero", "pos", "integral"}), {}),
    "str": (
        frozenset({"empty", "whitespace", "ascii", "unicode", "ws-edge", "digits"}),
        {"len": LEN_BUCKETS, "kind": STRING_KINDS},
    ),
    "bytes": (frozenset(), {"len": LEN_BUCKETS}),
    "list": (_CONTAINER_FLAGS, _CONTAINER_ATTRS),
    "tuple": (_CONTAINER_FLAGS, _CONTAINER_ATTRS),
    "set": (_CONTAINER_FLAGS, _CONTAINER_ATTRS),
    "frozenset": (_CONTAINER_FLAGS, _CONTAINER_ATTRS),
    "dict": (_CONTAINER_FLAGS, {**_CONTAINER_ATTRS, "key": None}),
    "obj": (frozenset({"dataclass", "pydantic"}), {}),
}
"""Per type token: the plain flags and the ``prefix:value`` attributes (allowed values, or None
for a type token) it may carry. ``obj`` stands for every ``obj:<module>.<qualname>`` type."""

_OBJ_NAME: Final = re.compile(r"[^\s|]+")
_TOKEN: Final = re.compile(r"[^\s|]+")
_CONTAINERS: Final = frozenset(CONTAINER_TYPES)


class ShapeKeyError(ValueError):
    """A shape key does not follow the grammar."""


@dataclass(frozen=True, slots=True)
class ParsedShape:
    """A shape key split into its type token, plain flags and ``prefix:value`` attributes."""

    type: str
    flags: frozenset[str]
    attrs: Mapping[str, frozenset[str]]

    def has(self, prefix: str, value: str) -> bool:
        """Whether the key carries the attribute token ``prefix:value``."""
        return value in self.attrs.get(prefix, ())

    @property
    def group(self) -> str:
        """The value-kind group: ``container`` for the five container types, else the type token."""
        if self.type in _CONTAINERS:
            return "container"
        return "object" if self.type.startswith("obj:") else self.type

    @property
    def type_name(self) -> str:
        """Short type name: ``none`` for NoneType, the class name for ``obj:`` types."""
        if self.type == "NoneType":
            return "none"
        return self.type.rsplit(".", 1)[-1] if self.type.startswith("obj:") else self.type


def parse(key: str) -> ParsedShape:
    """Parse ``<type>|<feature>|...``; ``prefix:value`` features go to ``attrs``.

    Raises :class:`ShapeKeyError` for an empty type token or an empty feature; it does not check
    the vocabulary (see :func:`validate`).
    """
    tokens = key.split("|")
    if not tokens[0]:
        raise ShapeKeyError(f"shape key has no type token: {key!r}")
    attrs: dict[str, set[str]] = defaultdict(set)
    flags: set[str] = set()
    for token in tokens[1:]:
        if not token:
            raise ShapeKeyError(f"empty feature token in shape key: {key!r}")
        prefix, sep, value = token.partition(":")
        if sep:
            attrs[prefix].add(value)
        else:
            flags.add(token)
    return ParsedShape(tokens[0], frozenset(flags), {k: frozenset(v) for k, v in attrs.items()})


parse_shape_key = parse


def _type_ok(token: str) -> bool:
    if token in SCALAR_TYPES or token in CONTAINER_TYPES:
        return True
    return token.startswith("obj:") and _OBJ_NAME.fullmatch(token[4:]) is not None


def validate(key: object) -> list[str]:
    """Problems with ``key`` under the version-1 vocabulary; an empty list means valid.

    Checked: a string of 1 to :data:`MAX_KEY_CHARS` printable characters without whitespace; a
    known type token (or ``obj:<name>``); only the features that type may carry, with known values
    (``elem:``/``key:`` take a type token or ``mixed``); at most one value per attribute; features
    sorted and not repeated.
    """
    if not isinstance(key, str):
        return ["a shape key is a string"]
    if not 0 < len(key) <= MAX_KEY_CHARS:
        return [f"a shape key has 1 to {MAX_KEY_CHARS} characters"]
    if not key.isprintable():
        return ["a shape key has printable characters only"]
    head, *features = key.split("|")
    if not _type_ok(head):
        return [f"unknown type token {head[:40]!r}"]
    problems: list[str] = []
    flags, attrs = FEATURES["obj" if head.startswith("obj:") else head]
    seen_attrs: set[str] = set()
    for token in features:
        if not _TOKEN.fullmatch(token):
            problems.append("empty or blank feature token")
            continue
        prefix, sep, value = token.partition(":")
        if not sep:
            if token not in flags:
                problems.append(f"{head.split(':')[0]} does not take the feature {token[:40]!r}")
            continue
        if prefix not in attrs:
            problems.append(f"{head.split(':')[0]} does not take {prefix[:40]!r}: features")
            continue
        if prefix in seen_attrs:
            problems.append(f"more than one {prefix}: feature")
        seen_attrs.add(prefix)
        allowed = attrs[prefix]
        if allowed is None:
            if value != "mixed" and not _type_ok(value):
                problems.append(f"{prefix}: needs a type token or mixed, got {value[:40]!r}")
        elif value not in allowed:
            problems.append(f"unknown {prefix}: value {value[:40]!r}")
    if features != sorted(features):
        problems.append("features are not sorted")
    if len(set(features)) != len(features):
        problems.append("a feature is repeated")
    return problems


def is_valid(key: object) -> bool:
    """Whether :func:`validate` finds nothing wrong with ``key``."""
    return not validate(key)


def canonical(key: str) -> str:
    """``key`` with its features sorted and de-duplicated (the type token stays first)."""
    head, *features = key.split("|")
    return "|".join((head, *sorted(set(features))))
