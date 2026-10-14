"""Sparklines: one or two percentage series over a few runs, as a small inline SVG.

Used for line % and input-class % over the default branch's baselines (the repo overview, the
dashboard's repository rows and the function page's history). The rules, from the dataviz method:

* one y-axis for both series (they are both percentages); the domain fits the data, padded to at
  least :data:`MIN_SPAN` points so a flat line does not look like a cliff, and clamped to 0–100;
* the first series is a solid line, the second a dashed one with a hollow end dot, so the two
  never differ by colour alone; :func:`render_spark_key` draws the matching key in words;
* the last value of each series is labelled at the right in text ink (pushed apart when the two
  labels would collide), other points are not labelled;
* every x position has a full-height hover target with a ``<title>`` naming the point and every
  series' value there; the SVG is ``role="img"`` with an ``aria-label`` that summarises each
  series (first → last over N points);
* ``None`` values are gaps: the line breaks there and an isolated value is drawn as a dot;
* an optional reference (a horizontal dotted rule with its own title) and an optional current
  index (a short tick under the point) mark a baseline level or the run being viewed.

Every text that reaches the markup goes through :func:`~coverset.web.render.html.esc` or
:func:`~coverset.web.render.html.attr`; geometry through :func:`~coverset.web.render.html.num`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from markupsafe import Markup

from coverset.units import fmt_pct
from coverset.web.render.html import attr, esc, num

__all__ = ["MIN_SPAN", "SparkSeries", "render_spark_key", "render_sparkline"]

MIN_WIDTH = 160
MAX_WIDTH = 240
HEIGHT = 36
LABEL_W = 34.0
"""Room at the right for the last-value labels."""
PAD_X = 4.0
PAD_Y = 5.0
MIN_SPAN = 10.0
"""The smallest y-domain in percentage points."""
LABEL_GAP = 10.0
"""Least vertical distance between the two last-value labels."""


@dataclass(frozen=True, slots=True)
class SparkSeries:
    """One series: a name for titles and the key, one value per x position (None = gap), and
    whether it is drawn dashed (a series keeps its style wherever it is drawn, so line % is solid
    and input-class % dashed on every page; with two series the second is always dashed)."""

    name: str
    values: tuple[float | None, ...]
    dashed: bool = False


def _styles(series: Sequence[SparkSeries]) -> list[str]:
    """``spark-s1`` (solid) or ``spark-s2`` (dashed) per series; two series never share one."""
    styles = ["spark-s2" if s.dashed else "spark-s1" for s in series]
    if len(styles) == 2 and styles[0] == styles[1]:
        styles = ["spark-s1", "spark-s2"]
    return styles


def _domain(values: Sequence[float]) -> tuple[float, float]:
    found = [v for v in values if math.isfinite(v)]
    if not found:
        return 0.0, 100.0
    lo, hi = min(found), max(found)
    if hi - lo < MIN_SPAN:
        mid = (lo + hi) / 2
        lo, hi = mid - MIN_SPAN / 2, mid + MIN_SPAN / 2
    if lo < 0:
        lo, hi = 0.0, hi - lo
    if hi > 100:
        lo, hi = max(0.0, lo - (hi - 100)), 100.0
    return lo, hi


def _clean(value: float | None) -> float | None:
    """A finite value clamped to [0, 100] (percentages), else None (a gap)."""
    if value is None or not math.isfinite(value):
        return None
    return min(100.0, max(0.0, float(value)))


_C0 = re.compile(r"[\x00-\x1f]")


def _text(value: object) -> str:
    """Label text without C0 control characters (not allowed in XML 1.0, so they would make the
    inline SVG not well-formed); tabs and line breaks become spaces."""
    return _C0.sub(lambda m: " " if m.group() in "\t\n\r" else "", str(value))


def _summary(series: Sequence[SparkSeries], count: int, noun: str) -> str:
    parts = []
    for s in series:
        present = [v for v in (_clean(v) for v in s.values) if v is not None]
        if not present:
            parts.append(f"{s.name}: no value")
        elif len(present) == 1:
            parts.append(f"{s.name} {fmt_pct(present[0])}")
        else:
            parts.append(f"{s.name} from {fmt_pct(present[0])} to {fmt_pct(present[-1])}")
    return f"{'; '.join(parts)} over {count} {noun}{'' if count == 1 else 's'}"


def render_sparkline(
    series: Sequence[SparkSeries],
    *,
    points: Sequence[str],
    width: int = 200,
    height: int = HEIGHT,
    label: str = "",
    noun: str = "run",
    current: int | None = None,
) -> Markup:
    """The sparkline SVG for one or two series over ``points`` (one label per x position, used in
    the hover titles). ``label`` prefixes the ``aria-label``; ``noun`` names a point there
    ("baseline"). Raises ``ValueError`` for zero or more than two series or a length mismatch."""
    if not 1 <= len(series) <= 2:
        raise ValueError("a sparkline draws one or two series")
    count = len(points)
    if any(len(s.values) != count for s in series):
        raise ValueError("every series needs one value per point")
    width = max(MIN_WIDTH, min(MAX_WIDTH, int(width)))
    height = max(24, int(height))
    plot_l, plot_r = PAD_X, width - LABEL_W
    plot_t, plot_b = PAD_Y, height - PAD_Y
    series = [SparkSeries(_text(s.name), s.values, s.dashed) for s in series]
    points = [_text(p) for p in points]
    label, noun = _text(label), _text(noun)
    values = [[_clean(v) for v in s.values] for s in series]
    lo, hi = _domain([v for vs in values for v in vs if v is not None])

    def x_at(i: int) -> float:
        return plot_r if count <= 1 else plot_l + (plot_r - plot_l) * i / (count - 1)

    def y_at(v: float) -> float:
        v = min(hi, max(lo, v))
        return plot_b - (plot_b - plot_t) * (v - lo) / (hi - lo)

    aria = _summary(series, count, noun)
    if label:
        aria = f"{label}: {aria}"
    parts: list[str] = [
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}"'
        f' role="img" aria-label="{attr(aria)}">',
        f'<line class="spark-base" x1="{num(plot_l)}" x2="{num(plot_r)}" y1="{num(plot_b)}" y2="{num(plot_b)}"/>',
    ]
    if current is not None and 0 <= current < count:
        x = x_at(current)
        parts.append(
            f'<line class="spark-current" x1="{num(x)}" x2="{num(x)}" y1="{num(plot_b)}" y2="{num(height - 1)}"/>'
        )
    styles = _styles(series)
    for index, vs in enumerate(values):
        cls = styles[index]
        segments: list[list[tuple[float, float]]] = [[]]
        for i, v in enumerate(vs):
            if v is None:
                if segments[-1]:
                    segments.append([])
                continue
            segments[-1].append((x_at(i), y_at(v)))
        for segment in segments:
            if len(segment) >= 2:
                d = " ".join(f"{'M' if j == 0 else 'L'}{num(x)} {num(y)}" for j, (x, y) in enumerate(segment))
                parts.append(f'<path class="spark-line {cls}" d="{d}"/>')
            elif len(segment) == 1:
                x, y = segment[0]
                parts.append(f'<circle class="spark-dot {cls}" cx="{num(x)}" cy="{num(y)}" r="2"/>')
    # The last value of each series: an end dot (hollow for the dashed series) and its label.
    ends: list[tuple[int, float, float, float]] = []
    for index, vs in enumerate(values):
        last = next(((i, v) for i, v in reversed(list(enumerate(vs))) if v is not None), None)
        if last is not None:
            i, v = last
            ends.append((index, x_at(i), y_at(v), v))
    label_ys = [min(max(y + 3.5, 9.0), height - 2.0) for _, _, y, _ in ends]
    if len(label_ys) == 2 and abs(label_ys[0] - label_ys[1]) < LABEL_GAP:
        upper = 0 if label_ys[0] <= label_ys[1] else 1
        mid = (label_ys[0] + label_ys[1]) / 2
        top = min(max(mid - LABEL_GAP / 2, 9.0), height - 2.0 - LABEL_GAP)
        label_ys[upper], label_ys[1 - upper] = top, top + LABEL_GAP
    for (index, x, y, v), ly in zip(ends, label_ys, strict=True):
        cls = styles[index]
        parts.append(f'<circle class="spark-end {cls}" cx="{num(x)}" cy="{num(y)}" r="2.75"/>')
        parts.append(f'<text class="spark-val" x="{num(plot_r + 6)}" y="{num(ly)}">{esc(fmt_pct(v))}</text>')
    # Hover targets: one full-height column per point, titled with every series' value there.
    step = (plot_r - plot_l) / (count - 1) if count > 1 else plot_r - plot_l
    for i in range(count):
        x = x_at(i)
        left = max(0.0, x - step / 2)
        right = min(float(width), x + step / 2) if count > 1 else float(width)
        title = " · ".join(
            [str(points[i]), *(f"{s.name} {fmt_pct(vs[i])}" for s, vs in zip(series, values, strict=True))]
        )
        parts.append(
            f'<rect class="spark-hit" x="{num(left)}" y="0" width="{num(max(1.0, right - left))}"'
            f' height="{height}"><title>{esc(title)}</title></rect>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))


def render_spark_key(series: Sequence[SparkSeries]) -> Markup:
    """The key for a sparkline's series: a short solid or dashed line (as the sparkline draws
    that series) and the name, so identity never rests on colour."""
    items = []
    for cls, s in zip(_styles(series[:2]), series[:2], strict=True):
        name = s.name
        items.append(
            '<span class="spark-key-item"><svg class="spark-swatch" viewBox="0 0 18 8" width="18" height="8"'
            f' aria-hidden="true"><line class="spark-line {cls}" x1="1" x2="17" y1="4" y2="4"/></svg>'
            f"{esc(_text(name))}</span>"
        )
    return Markup(f'<span class="spark-key">{"".join(items)}</span>')
