"""Redacted example reprs for recorded argument shapes.

Examples are produced with a :class:`reprlib.Repr` (``maxstring=48, maxother=48, maxlevel=2``) that
only ever formats exact builtin types; anything else prints as ``<module.Class>`` without calling its
``__repr__``. A value is redacted to ``<redacted:len=N>`` (``<redacted>`` when it has no length) when
the name it is bound to looks secret: the parameter name, the function name for the pseudo-parameter
``return`` (``make_reset_token`` returns a token), or a ``str`` dict key inside a container example
(``{'password': ...}``). Every string is scrubbed with the same credential patterns as the control
plane's ``coverset.logging`` *before* reprlib shortens it, and the finished text once more; every
email keeps its domain but loses its local part (``a***@sub.example.com``). Ints wider than :data:`MAX_INT_BITS` bits and long bytes are summarised
without building their full repr.
"""

from __future__ import annotations

import re
import reprlib
from itertools import islice
from typing import Literal

from pytest_coverset.features import BUILTIN_IDS, CONTAINER_IDS, SCALAR_IDS, class_name

ReprMode = Literal["off", "scalars", "all"]

MAX_EXAMPLE_CHARS = 200
MAX_INT_BITS = 128
RETURN_PARAM = "return"

SECRET_NAME_RE = re.compile(r"(?i)pass|secret|token|key|auth|cookie|session|cred")
"""Parameter, function and dict-key names whose values are never shown."""

SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    (re.compile(r"(?i)(x-access-token:)[^@\s]+@"), r"\1[REDACTED]@"),
    (re.compile(r"\b(gh[opsur]_)[A-Za-z0-9._-]{20,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(github_pat_)[A-Za-z0-9_]{20,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(sk-ant-)[A-Za-z0-9_-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(authorization:\s*(?:bearer|token)\s+)[^\s\"',;]+"), r"\1[REDACTED]"),
)
"""Credential shapes masked in example text (kept in sync with ``coverset.logging.PATTERNS``)."""

_EMAIL_LOCAL_RE = re.compile(r"([^\s@'\"\\,(){}\[\]<>])[^\s@'\"\\,(){}\[\]<>]*@(?=[^\s@'\"]+\.[^\s@'\"]+)")


def _redacted(value: object) -> str:
    cls = type(value)
    if cls is str or cls is bytes or id(cls) in CONTAINER_IDS:
        return f"<redacted:len={len(value)}>"  # type: ignore[arg-type]
    return "<redacted>"


class SafeRepr(reprlib.Repr):
    """A ``reprlib.Repr`` that formats exact builtin types only and never sorts or hashes elements."""

    def __init__(self) -> None:
        super().__init__(maxstring=48, maxother=48, maxlevel=2)

    def repr1(self, x: object, level: int) -> str:
        if id(type(x)) not in BUILTIN_IDS:
            return "<" + class_name(type(x)) + ">"
        return super().repr1(x, level)

    def repr_str(self, x: str, level: int) -> str:
        # Scrub before reprlib cuts the middle out: scrubbing only the shortened text would leave
        # the tail of a token, or an email's local part, that the cut separated from its prefix.
        return super().repr_str(_scrub_raw(x), level)

    def repr_int(self, x: int, level: int) -> str:
        bits = x.bit_length()
        if bits > MAX_INT_BITS:
            return f"<int {bits} bits>"
        return super().repr_int(x, level)

    def repr_bytes(self, x: bytes, level: int) -> str:
        if len(x) <= self.maxstring:
            return repr(x)
        head = max(0, (self.maxstring - 3) // 2)
        tail = max(0, self.maxstring - 3 - head)
        return repr(x[:head]) + self.fillvalue + repr(x[len(x) - tail :])

    def _items(self, items: list[str], n: int, limit: int, left: str, right: str, trail: str = "") -> str:
        if n > limit:
            items.append(self.fillvalue)
        return left + ", ".join(items) + (trail if n == 1 else "") + right

    def repr_set(self, x: set, level: int) -> str:
        if not x:
            return "set()"
        if level <= 0:
            return "{" + self.fillvalue + "}"
        return self._items(
            [self.repr1(e, level - 1) for e in islice(x, self.maxset)], len(x), self.maxset, "{", "}"
        )

    def repr_frozenset(self, x: frozenset, level: int) -> str:
        if not x:
            return "frozenset()"
        if level <= 0:
            return "frozenset({" + self.fillvalue + "})"
        inner = [self.repr1(e, level - 1) for e in islice(x, self.maxfrozenset)]
        return self._items(inner, len(x), self.maxfrozenset, "frozenset({", "})")

    def repr_dict(self, x: dict, level: int) -> str:
        if not x:
            return "{}"
        if level <= 0:
            return "{" + self.fillvalue + "}"
        pieces = [
            f"{self.repr1(k, level - 1)}: {self._value_repr(k, v, level - 1)}"
            for k, v in islice(x.items(), self.maxdict)
        ]
        return self._items(pieces, len(x), self.maxdict, "{", "}")

    def _value_repr(self, key: object, value: object, level: int) -> str:
        if type(key) is str and SECRET_NAME_RE.search(key):
            return _redacted(value)
        return self.repr1(value, level)


_SAFE_REPR = SafeRepr()


def scrub(text: str) -> str:
    """Mask credential shapes and email local parts in ``text``."""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return _EMAIL_LOCAL_RE.sub(r"\1***@", text)


SCRUB_WINDOW = 4096
"""Strings longer than twice this are scrubbed as head + tail windows (only their ends are shown)."""


def _scrub_raw(text: str) -> str:
    """Scrub a raw string before it is shortened.

    reprlib shows only the first and last two dozen characters, so a very long string is scrubbed as
    two :data:`SCRUB_WINDOW` windows (bounded work). A PEM private key is the one secret long enough
    to span a window, so a string containing one is scrubbed whole (the PEM pattern then masks it to
    its END line or the end of the text).
    """
    if len(text) <= 2 * SCRUB_WINDOW or "PRIVATE KEY-----" in text:
        return scrub(text)
    return scrub(text[:SCRUB_WINDOW]) + " " + scrub(text[-SCRUB_WINDOW:])


def example_repr(param: str, value: object, mode: ReprMode, *, func_name: str | None = None) -> str | None:
    """Return a short redacted repr of ``value`` for parameter ``param``, or None when not recorded.

    ``scalars`` records None/bool/int/float/str/bytes only; ``all`` adds builtin containers. For the
    pseudo-parameter ``return``, ``func_name`` (the function's ``co_name``) decides whether the value
    is secret.
    """
    if mode == "off":
        return None
    if id(type(value)) not in (SCALAR_IDS if mode == "scalars" else BUILTIN_IDS):
        return None
    name = func_name if param == RETURN_PARAM and func_name is not None else param
    if SECRET_NAME_RE.search(name):
        return _redacted(value)
    return scrub(_SAFE_REPR.repr(value))[:MAX_EXAMPLE_CHARS]
