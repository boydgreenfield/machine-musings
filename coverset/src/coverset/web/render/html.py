"""Escaping and small SVG helpers shared by the renderers.

``esc`` escapes text content (``&``, ``<``, ``>``, both quotes), ``attr`` an attribute value (the
same, and it never emits a raw quote, so ``"onmouseover=`` stays inside the attribute), and
``safe_href`` admits only same-site paths and absolute http(s) URLs (``javascript:`` and friends
become ``#``). Numbers in SVG geometry go through :func:`num` so a NaN or inf never reaches markup.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

from markupsafe import Markup, escape

from coverset.units import fmt_ms, fmt_secs

__all__ = [
    "Markup",
    "attr",
    "attrs",
    "breakable",
    "esc",
    "fmt_ms",
    "fmt_secs",
    "join",
    "num",
    "safe_href",
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
    """``url`` when it is a same-site path or an absolute http(s) URL, else ``#``."""
    if not url:
        return "#"
    if _SAFE_PATH.match(url) or _SAFE_URL.match(url):
        return url
    return "#"


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


_JOINTS = re.compile(r"(?<=::)|(?<=/)|(?<=\[)|(?<=_)|(?<=-)")


def breakable(text: object) -> Markup:
    """Escaped ``text`` with ``<wbr>`` after ``::``, ``/``, ``[``, ``_`` and ``-``, so a long test
    id wraps at its natural joints instead of mid-word."""
    return Markup("<wbr>").join(esc(part) for part in _JOINTS.split("" if text is None else str(text)))
