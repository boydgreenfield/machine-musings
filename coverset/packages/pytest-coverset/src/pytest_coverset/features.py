"""Value features and canonical shape keys (plan Addendum B).

A shape key is ``<type>`` followed by ``|``-joined feature tokens sorted alphabetically, e.g.
``str|ascii|kind:email/multi-label-domain|len:11-100``. Features are computed only for values whose
type is exactly one of the builtin types below; anything else is described by its class name alone.
No user-defined ``__len__``, ``__iter__``, ``__bool__``, ``__eq__``, ``__hash__`` or ``__repr__`` is ever
called: containers are sampled by indexing (list, tuple) or builtin iteration (set, dict) over at most
:data:`MAX_ELEMENTS` items, and element features are computed only for exact builtin element types.

The numeric sign classes ``neg|zero|pos`` describe finite values only: ``+inf`` is ``float|inf`` and
``-inf`` is ``float|inf|inf-neg``, so an infinity never counts as exercising the finite sign classes.
"""

from __future__ import annotations

import math
import re
from itertools import islice

NoneType = type(None)

MAX_ELEMENTS = 32
"""Containers are summarised over at most this many leading elements."""

MAX_KIND_CHARS = 2048
"""Strings longer than this get no ``kind:`` token (no email or URL is that long)."""

SCAN_CHARS = 4096
"""Whitespace and digit features of longer strings are computed over this prefix."""

BIG_INT = 2**31

SCALAR_TYPES: frozenset[type] = frozenset({NoneType, bool, int, float, str, bytes})
CONTAINER_TYPES: frozenset[type] = frozenset({list, tuple, dict, set, frozenset})
BUILTIN_TYPES: frozenset[type] = SCALAR_TYPES | CONTAINER_TYPES

# Membership tests on classes go through these id sets: ``cls in BUILTIN_TYPES`` would hash ``cls``
# and ``type(x) in (int, float)`` would compare it, calling a user metaclass's ``__hash__``/``__eq__``.
SCALAR_IDS: frozenset[int] = frozenset(map(id, SCALAR_TYPES))
CONTAINER_IDS: frozenset[int] = frozenset(map(id, CONTAINER_TYPES))
BUILTIN_IDS: frozenset[int] = SCALAR_IDS | CONTAINER_IDS


def _is_number(value: object) -> bool:
    cls = type(value)
    return cls is int or cls is float


TWO_LEVEL_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
        "com.au", "net.au", "org.au", "edu.au", "gov.au",
        "co.jp", "ne.jp", "or.jp", "ac.jp",
        "co.nz", "org.nz", "co.za", "co.in", "co.id", "co.il", "co.kr",
        "com.br", "com.mx", "com.ar", "com.sg", "com.hk", "com.tw", "com.tr", "com.pl",
        "com.my", "com.ph", "com.vn", "com.eg", "com.pk", "com.ng", "com.cn", "com.co",
    }
)  # fmt: skip
"""Common two-level public suffixes counted as one label when deciding ``multi-label-domain``."""

KNOWN_UNITS = frozenset(
    {
        "ns", "us", "µs", "ms", "s", "sec", "secs", "second", "seconds",
        "m", "min", "mins", "minute", "minutes", "h", "hr", "hrs", "hour", "hours",
        "d", "day", "days", "w", "wk", "wks", "week", "weeks", "y", "yr", "yrs", "year", "years",
        "b", "kb", "mb", "gb", "tb", "kib", "mib", "gib", "tib", "k",
        "mm", "cm", "km", "in", "ft", "mi", "g", "kg", "mg", "lb", "lbs", "oz",
        "px", "pt", "em", "rem",
    }
)  # fmt: skip
"""Units a quantity string may use without counting as trailing garbage (matched case-insensitively)."""

_EMAIL_RE = re.compile(r"(?P<local>[^@\s]+)@(?P<domain>[^@\s]+\.[^@\s]+)")
_QUANTITY_RE = re.compile(r"\s*[-+]?[\d.]+\s*[A-Za-z]+")
_QUANTITY_HEAD_RE = re.compile(r"\s*(?P<sign>[-+]?)")
_QUANTITY_SEGMENT_RE = re.compile(r"\s*(?P<num>\d+(?:\.\d*)?|\.\d+)\s*(?P<unit>[^\W\d_]+)")
_NUMERIC_RE = re.compile(r"\s*[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?\s*")
_ISO_DATE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_DIGIT_RE = re.compile(r"\d")


def len_bucket(n: int) -> str:
    """Bucket a length into ``0|1|2-10|11-100|101-1000|>1000``."""
    if n <= 1:
        return str(n)
    if n <= 10:
        return "2-10"
    if n <= 100:
        return "11-100"
    if n <= 1000:
        return "101-1000"
    return ">1000"


def class_name(cls: type) -> str:
    """``module.qualname`` of ``cls`` read through ``type``'s own descriptors (metaclasses cannot interfere)."""
    module = _TYPE_MODULE.__get__(cls)
    qualname = _TYPE_QUALNAME.__get__(cls)
    # ``type(...) is str``, not isinstance(): a class body can set ``__module__`` to any object.
    return f"{module}.{qualname}" if type(module) is str and module else str(qualname)


_TYPE_MODULE = type.__dict__["__module__"]
_TYPE_QUALNAME = type.__dict__["__qualname__"]
_TYPE_MRO = type.__dict__["__mro__"]
_TYPE_DICT = type.__dict__["__dict__"]
_TYPE_NAME = type.__dict__["__name__"]


def type_token(cls: type) -> str:
    """The type token of the shape grammar: a builtin name or ``obj:<module>.<qualname>``."""
    if id(cls) in BUILTIN_IDS:
        return _TYPE_NAME.__get__(cls)
    return "obj:" + class_name(cls)


def _object_features(cls: type) -> tuple[str, ...]:
    for klass in _TYPE_MRO.__get__(cls):
        namespace = _TYPE_DICT.__get__(klass)
        if "__dataclass_fields__" in namespace:
            return ("dataclass",)
        module = _TYPE_MODULE.__get__(klass)
        if (
            _TYPE_QUALNAME.__get__(klass) == "BaseModel"
            and type(module) is str
            and module.startswith("pydantic")
        ):
            return ("pydantic",)
    return ()


def object_shape_key(cls: type) -> str:
    """Shape key of a non-builtin value or a receiver class: ``obj:<module>.<qualname>`` plus model flags."""
    return _join("obj:" + class_name(cls), _object_features(cls))


def shape_key(value: object) -> str:
    """Return the canonical shape key of ``value`` (see the module docstring for the safety rules)."""
    cls = type(value)
    if cls is NoneType:
        return "NoneType"
    if cls is bool:
        return "bool|true" if value else "bool|false"
    if cls is int:
        return _join("int", _int_features(value))  # type: ignore[arg-type]
    if cls is float:
        return _join("float", _float_features(value))  # type: ignore[arg-type]
    if cls is str:
        return _join("str", _str_features(value))  # type: ignore[arg-type]
    if cls is bytes:
        return _join("bytes", ("len:" + len_bucket(len(value)),))  # type: ignore[arg-type]
    if cls is list or cls is tuple:
        n = len(value)  # type: ignore[arg-type]
        sample = [value[i] for i in range(min(n, MAX_ELEMENTS))]  # type: ignore[index]
        return _join(cls.__name__, _sequence_features(n, sample))
    if cls is set or cls is frozenset:
        n = len(value)  # type: ignore[arg-type]
        sample = list(islice(value, MAX_ELEMENTS))  # type: ignore[call-overload]
        return _join(cls.__name__, _collection_features(n, sample))
    if cls is dict:
        return _join("dict", _dict_features(value))  # type: ignore[arg-type]
    return object_shape_key(cls)


def _join(head: str, features: tuple[str, ...] | list[str]) -> str:
    return "|".join((head, *sorted(features))) if features else head


def _int_features(value: int) -> tuple[str, ...]:
    sign = "neg" if value < 0 else ("zero" if value == 0 else "pos")
    return (sign, "big") if abs(value) > BIG_INT else (sign,)


def _float_features(value: float) -> tuple[str, ...]:
    if value != value:
        return ("nan",)
    if math.isinf(value):
        return ("inf", "inf-neg") if value < 0 else ("inf",)
    sign = "neg" if value < 0 else ("zero" if value == 0 else "pos")
    return (sign, "integral") if value.is_integer() else (sign,)


def _str_features(value: str) -> list[str]:
    n = len(value)
    feats = ["len:" + len_bucket(n)]
    if n == 0:
        feats.append("empty")
        return feats
    head = value if n <= SCAN_CHARS else value[:SCAN_CHARS]
    if head.isspace():
        feats.append("whitespace")
        return feats
    feats.append("ascii" if value.isascii() else "unicode")
    if value[0].isspace() or value[-1].isspace():
        feats.append("ws-edge")
    if _DIGIT_RE.search(head):
        feats.append("digits")
    if n <= MAX_KIND_CHARS:
        kind = string_kind(value)
        if kind is not None:
            feats.append("kind:" + kind)
    return feats


def string_kind(value: str) -> str | None:
    """Classify a string: ``email/<sub>``, ``numeric``, ``quantity/<sub>``, ``iso-date`` or ``url`` (first match)."""
    match = _EMAIL_RE.fullmatch(value)
    if match is not None and all(match.group("domain").split(".")):
        return "email/" + email_subclass(match.group("local"), match.group("domain"))
    if _NUMERIC_RE.fullmatch(value):
        return "numeric"
    if _QUANTITY_RE.match(value):
        return "quantity/" + quantity_subclass(value)
    if _ISO_DATE_RE.fullmatch(value):
        return "iso-date"
    if _URL_RE.fullmatch(value):
        return "url"
    return None


def email_subclass(local: str, domain: str) -> str:
    """``idn`` > ``multi-label-domain`` > ``plus-tag`` > ``uppercase`` > ``plain`` (first match wins)."""
    labels = domain.lower().split(".")
    if not (local.isascii() and domain.isascii()) or any(label.startswith("xn--") for label in labels):
        return "idn"
    n_labels = (
        len(labels) - 1 if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES else len(labels)
    )
    if n_labels >= 3:
        return "multi-label-domain"
    if "+" in local:
        return "plus-tag"
    if local != local.lower() or domain != domain.lower():
        return "uppercase"
    return "plain"


def quantity_subclass(value: str) -> str:
    """Sub-class a string that starts like ``<number><unit>``.

    Precedence: ``trailing-garbage`` (unparsed text or an unknown unit that extends a known one, e.g.
    ``1hx``) > ``signed`` > ``multi-unit`` (``1h30m``) > ``decimal`` (``1.5h``) > ``multi-letter-unit``
    (``100ms``) > ``single-unit`` (``5m``).
    """
    head = _QUANTITY_HEAD_RE.match(value)
    pos = head.end() if head is not None else 0
    signed = bool(head and head.group("sign"))
    units: list[str] = []
    decimal = False
    while True:
        segment = _QUANTITY_SEGMENT_RE.match(value, pos)
        if segment is None:
            break
        units.append(segment.group("unit"))
        decimal = decimal or "." in segment.group("num")
        pos = segment.end()
    if value[pos:].strip() or not units or any(_garbage_unit(unit) for unit in units):
        return "trailing-garbage"
    if signed:
        return "signed"
    if len(units) > 1:
        return "multi-unit"
    if decimal:
        return "decimal"
    return "multi-letter-unit" if len(units[0]) > 1 else "single-unit"


def _garbage_unit(unit: str) -> bool:
    lowered = unit.lower()
    if lowered in KNOWN_UNITS:
        return False
    return any(lowered.startswith(known) for known in KNOWN_UNITS)


def _elem_token(sample: list[object]) -> str | None:
    if not sample:
        return None
    first = type(sample[0])
    for item in sample:
        if type(item) is not first:
            return "elem:mixed"
    return "elem:" + type_token(first)


def _numeric_features(sample: list[object]) -> list[str]:
    feats: list[str] = []
    if not sample or not all(_is_number(x) for x in sample):
        return feats
    numbers: list[float | int] = sample  # type: ignore[assignment]
    zeros = [x == 0 for x in numbers]
    if any(zeros):
        feats.append("any_zero")
    if all(zeros):
        feats.append("all_zero")
    if any(x < 0 for x in numbers):
        feats.append("any_neg")
    if any(x != x for x in numbers):
        feats.append("any_nan")
    if any(type(x) is float and math.isinf(x) for x in numbers):
        feats.append("any_inf")
    try:
        total = sum(numbers)
    except OverflowError:  # a huge int next to a float: the sum has no float value
        return feats
    if total == 0:
        feats.append("sum_zero")
    return feats


def _numeric_pair(item: object) -> tuple[float | int, float | int] | None:
    cls = type(item)
    if (cls is tuple or cls is list) and len(item) == 2:  # type: ignore[arg-type]
        a, b = item[0], item[1]  # type: ignore[index]
        if _is_number(a) and _is_number(b):
            return (a, b)
    return None


def _pair_features(sample: list[object]) -> tuple[list[str], list[tuple[float | int, float | int]] | None]:
    pairs = [_numeric_pair(item) for item in sample]
    if not sample or any(p is None for p in pairs):
        return [], None
    valid: list[tuple[float | int, float | int]] = pairs  # type: ignore[assignment]
    orders = {"lt" if a < b else "eq" if a == b else "gt" if a > b else "nan" for a, b in valid}
    label = orders.pop() if len(orders) == 1 else "mixed"
    return ["pair:" + ("mixed" if label == "nan" else label)], valid


def _comparable(sample: list[object]) -> list | None:
    """The sample in a form that compares with builtin semantics only, or None when it cannot."""
    if all(_is_number(x) for x in sample):
        return None if any(x != x for x in sample) else sample
    if all(type(x) is str for x in sample) or all(type(x) is bytes for x in sample):
        return sample
    return None


def _order_features(values: list) -> list[str]:
    feats: list[str] = []
    pairs = list(zip(values, values[1:], strict=False))
    if all(a <= b for a, b in pairs):
        feats.append("order:sorted")
    elif all(a >= b for a, b in pairs):
        feats.append("order:reverse")
    else:
        feats.append("order:unsorted")
    if len(set(values)) < len(values):
        feats.append("dups")
    return feats


def _sequence_features(n: int, sample: list[object]) -> list[str]:
    feats = _collection_features(n, sample)
    pair_feats, pairs = _pair_features(sample)
    feats.extend(pair_feats)
    if len(sample) < 2:
        return feats
    comparable: list | None
    if pairs is not None:
        comparable = None if any(a != a or b != b for a, b in pairs) else pairs
    else:
        comparable = _comparable(sample)
    feats.extend(_order_features(comparable) if comparable is not None else ["order:n/a"])
    return feats


def _collection_features(n: int, sample: list[object]) -> list[str]:
    feats = ["len:" + len_bucket(n)]
    elem = _elem_token(sample)
    if elem is not None:
        feats.append(elem)
    feats.extend(_numeric_features(sample))
    return feats


def _dict_features(value: dict) -> list[str]:
    n = len(value)
    keys = list(islice(value, MAX_ELEMENTS))
    values = list(islice(value.values(), MAX_ELEMENTS))
    feats = _collection_features(n, values)
    key = _elem_token(keys)
    if key is not None:
        feats.append("key:" + key.removeprefix("elem:"))
    return feats
