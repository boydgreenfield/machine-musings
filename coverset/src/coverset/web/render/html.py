"""Escaping and small SVG helpers shared by the renderers.

``esc`` escapes text content (``&``, ``<``, ``>``, both quotes), ``attr`` an attribute value (the
same, and it never emits a raw quote, so ``"onmouseover=`` stays inside the attribute), and
``safe_href`` admits only same-site paths and absolute http(s) URLs (``javascript:`` and friends
become ``#``). Numbers in SVG geometry go through :func:`num` so a NaN or inf never reaches markup.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from markupsafe import Markup, escape

from coverset.units import fmt_ms, fmt_secs

__all__ = [
    "Markup",
    "mark_bidi",
    "attr",
    "attrs",
    "breakable",
    "esc",
    "fmt_ms",
    "fmt_secs",
    "join",
    "json_data",
    "num",
    "relative_hrefs",
    "safe_href",
    "safe_relative_href",
    "truncate",
]

_SAFE_PATH = re.compile(r"^/(?!/)[^\s\\\x00-\x1f\x7f]*$")
_SAFE_URL = re.compile(r"^https?://[^\s<>\"'`\\\x00-\x1f\x7f]+$", re.IGNORECASE)


def esc(value: object) -> Markup:
    """``value`` as escaped text content."""
    return escape("" if value is None else str(value))


def attr(value: object) -> Markup:
    """``value`` escaped for a double-quoted attribute value (quotes become entities)."""
    return escape("" if value is None else str(value))


def attrs(**values: object) -> Markup:
    """`` name="value"`` pairs for every value that is not None (``_`` in names becomes ``-``).

    ``True`` renders a bare boolean attribute; ``False`` and None are left out.
    """
    parts: list[str] = []
    for name, value in values.items():
        key = name.rstrip("_").replace("_", "-")
        if value is None or value is False:
            continue
        if value is True:
            parts.append(f" {key}")
        else:
            parts.append(f' {key}="{attr(value)}"')
    return Markup("".join(parts))


def safe_href(url: str | None) -> str:
    """``url`` when it is a same-site path or an absolute http(s) URL, else ``#``.

    Inside :func:`relative_hrefs` (the static report) a relative link to a generated ``.html`` page
    (or a bare ``#fragment``) is admitted too, see :func:`safe_relative_href`.
    """
    if not url:
        return "#"
    if _SAFE_PATH.match(url) or _SAFE_URL.match(url):
        return url
    if _RELATIVE.get() and _SAFE_RELATIVE.match(url):
        return url
    return "#"


# -- static report helpers (additive: the live pages never enter relative_hrefs) -------------------

_RELATIVE: ContextVar[bool] = ContextVar("coverset_relative_hrefs", default=False)
_SEGMENT = r"[A-Za-z0-9_~-][A-Za-z0-9._~-]*"
_FRAGMENT = r"(?:#[A-Za-z0-9._~:=-]*)?"
_SAFE_RELATIVE = re.compile(
    rf"^(?:(?:\.\./)*(?:{_SEGMENT}/)*{_SEGMENT}\.html{_FRAGMENT}|#[A-Za-z0-9._~:=-]*)$"
)
_JSON_ESCAPES = {ord("<"): "\\u003c", ord(">"): "\\u003e", ord("&"): "\\u0026"}


def safe_relative_href(url: str | None) -> str:
    """``url`` when it is a relative link to a generated ``.html`` page or a ``#fragment``, else ``#``.

    Segments are plain names (no leading dot, so no ``..`` except as leading ``../`` steps, no
    scheme, no ``//``, no backslash or quote): ``../files/src/a.py.html#L12`` passes,
    ``javascript:alert(1)`` and ``//evil.example/x.html`` do not.
    """
    if url and _SAFE_RELATIVE.match(url):
        return url
    return "#"


@contextmanager
def relative_hrefs() -> Iterator[None]:
    """While active, :func:`safe_href` also admits :func:`safe_relative_href` links (the static
    report's pages link to each other relatively so they work from ``file://``)."""
    token = _RELATIVE.set(True)
    try:
        yield
    finally:
        _RELATIVE.reset(token)


def json_data(value: object) -> Markup:
    """``value`` as JSON safe inside ``<script type="application/json">``: ``<``, ``>`` and ``&``
    are written as ``\\u003c`` escapes, so no string can close the element or open a comment."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False, allow_nan=False)
    return Markup(text.translate(_JSON_ESCAPES))


def num(value: float, digits: int = 2) -> str:
    """A finite number for SVG geometry, without trailing zeros (non-finite values become 0)."""
    if not math.isfinite(value):
        value = 0.0
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def truncate(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters with an ellipsis."""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def join(parts: Iterable[Markup | str]) -> Markup:
    """Concatenate already-safe pieces (plain strings are escaped)."""
    return Markup("").join(p if isinstance(p, Markup) else escape(p) for p in parts)


_BIDI = re.compile("[؜‎‏‪-‮⁦-⁩]")


def mark_bidi(text: object) -> str:
    """``text`` (plain, unescaped) with each bidirectional control character replaced by a visible
    ``<U+202E>`` marker, so a Trojan-Source string in a test id, a path or a label shows what it is
    instead of reordering the line. Applied before escaping; the report applies the same rule to
    whole pages."""
    return _BIDI.sub(lambda m: f"<U+{ord(m.group()):04X}>", "" if text is None else str(text))


_JOINTS = re.compile(r"(?<=::)|(?<=/)|(?<=\[)|(?<=_)|(?<=-)")


def breakable(text: object) -> Markup:
    """Escaped ``text`` with ``<wbr>`` after ``::``, ``/``, ``[``, ``_`` and ``-``, so a long test
    id wraps at its natural joints instead of mid-word."""
    return Markup("<wbr>").join(esc(part) for part in _JOINTS.split(mark_bidi(text)))
