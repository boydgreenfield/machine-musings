"""Human formatting of durations, shared by the web pages and the sticky comment.

Both surfaces must print the same string for the same value (the PR page's "first failure 1.8 s"
and the comment's "first failure in 1.8 s" come from one rule), so the helpers live here, outside
``coverset.web``, where ``coverset.forge`` can import them without depending on the UI.
"""

from __future__ import annotations

__all__ = ["fmt_ms", "fmt_secs"]


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
