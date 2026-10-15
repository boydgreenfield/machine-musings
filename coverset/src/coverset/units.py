"""Human formatting shared by the web pages, the static report, the sticky comment and the CLI.

Every surface must print the same string for the same value (the PR page's "first failure 1.8 s"
and the comment's "first failure in 1.8 s" come from one rule), so the helpers live here, outside
``coverset.web``, where ``coverset.forge`` and the CLI can import them without depending on the UI.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

__all__ = ["fmt_ms", "fmt_pct", "fmt_ranges", "fmt_secs", "line_ranges", "ordinal", "plural"]


def fmt_secs(value: float | None) -> str:
    """Seconds as every page shows them: ``1.0 s``, ``41 s``, ``3 min`` (``–`` for None)."""
    if value is None:
        return "–"
    if value < 10:
        return f"{value:.1f} s"
    if value < 600:
        return f"{value:.0f} s"
    return f"{value / 60:.0f} min"


def fmt_ms(value: float | None) -> str:
    """Milliseconds as ``0.4 ms``, ``12 ms`` or ``1.4 s`` (``–`` for None)."""
    if value is None:
        return "–"
    if value < 10:
        return f"{value:.1f} ms"
    if value < 1000:
        return f"{value:.0f} ms"
    return f"{value / 1000:.1f} s"


def fmt_pct(value: float | None) -> str:
    """``90%`` or ``33.8%`` (one decimal when it matters), ``n/a`` for None."""
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.0f}%" if abs(value - round(value)) < 0.05 else f"{value:.1f}%"


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """``1 test``, ``2 tests`` (``plural_form`` when adding an ``s`` is wrong)."""
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"


def ordinal(number: int | None) -> str:
    """``1st``, ``66th`` (``–`` for None)."""
    if number is None:
        return "–"
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def line_ranges(lines: Iterable[int]) -> list[tuple[int, int]]:
    """Line numbers folded into sorted inclusive ``(first, last)`` runs."""
    out: list[tuple[int, int]] = []
    for line in sorted(set(lines)):
        if out and line == out[-1][1] + 1:
            out[-1] = (out[-1][0], line)
        else:
            out.append((line, line))
    return out


def fmt_ranges(lines: Iterable[int]) -> list[str]:
    """``["3-5", "9", "12-13"]``."""
    return [f"{a}" if a == b else f"{a}-{b}" for a, b in line_ranges(lines)]
