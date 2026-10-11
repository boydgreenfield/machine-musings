"""The shape grid: one row per parameter (plus ``return``), one square per input class.

A **filled** square is an input class some test passed in; its shade steps with log10(samples).
A **hollow dashed** square is a class the catalogue expected that no test ever passed in (the
"missing reason" wording, "never exercised" or "not observed in N sampled calls (K tests hit the
sample cap)", comes from :class:`~coverset.analysis.shapes.FunctionInputCov`). Classes are grouped by
family (type, number, str, email, quantity, container, elements, ...). Every square is a button
that loads the cell's detail fragment (tests and a redacted example, or a Hypothesis suggestion).
The mini strip is the same squares in one line, for cards.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from markupsafe import Markup

from coverset.web.render.html import attr, esc, num, safe_href, truncate

__all__ = ["GridCell", "GridModel", "GridRow", "render_grid", "render_inlay", "render_strip", "shade_level"]

CELL = 14.0
LABEL_W = 150.0
CHAR_W = 6.6
FAMILY_CHAR_W = 5.8
CELL_GAP = 14.0
GROUP_GAP = 18.0
LINE_H = 22.0
CAPTION_H = 14.0
ROW_GAP = 12.0


@dataclass(frozen=True, slots=True)
class GridCell:
    """One input class of one parameter."""

    name: str
    """Full class name ``<param>:<class>``."""
    family: str
    observed: bool
    samples: int = 0
    hint: str = ""
    description: str = ""
    role: str = "input"
    """``input`` (a parameter's class) or ``return`` (a class of the returned value: observed only)."""

    @property
    def is_return(self) -> bool:
        """Whether this cell describes the returned value."""
        return self.role == "return"

    @property
    def short(self) -> str:
        """The class part of the name."""
        return self.name.split(":", 1)[1] if ":" in self.name else self.name


@dataclass(frozen=True, slots=True)
class GridRow:
    """A parameter (or ``return``) and its expected classes."""

    param: str
    annotation: str | None
    cells: tuple[GridCell, ...]
    note: str = ""


@dataclass(frozen=True, slots=True)
class GridModel:
    """Everything the grid shows for one function."""

    func_key: str
    qualname: str
    rows: tuple[GridRow, ...]
    observed: int
    expected: int
    line_pct: float | None = None
    branch_pct: float | None = None
    missing_reason: str = "never exercised"
    note: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def header(self) -> str:
        """``Input classes 4 of 13 · lines 100% · branches 100%``."""
        parts = [f"Input classes {self.observed} of {self.expected}"]
        if self.line_pct is not None:
            parts.append(f"lines {self.line_pct:.0f}%")
        if self.branch_pct is not None:
            parts.append(f"branches {self.branch_pct:.0f}%")
        return " · ".join(parts)


def shade_level(samples: int) -> int:
    """1-4 by log10 of the sample count (1 sample → 1, 10 → 2, 100 → 3, 1000+ → 4)."""
    if samples <= 0:
        return 0
    return max(1, min(4, 1 + int(math.log10(samples))))


def _label_w(text: str) -> float:
    return len(text) * CHAR_W


def render_grid(
    model: GridModel,
    *,
    cell_url: Callable[[GridCell], str],
    target: str,
    width: float = 860.0,
) -> Markup:
    """The grid block: header line, SVG and the hollow-cell wording.

    ``width`` is the SVG's coordinate width; size it to the grid's container (the stylesheet scales
    the SVG down, never up, so it never overflows a narrower column).
    """
    parts: list[str] = []
    y = 4.0
    max_x = width - 8
    for index, row in enumerate(model.rows):
        label = row.param if row.annotation is None else f"{row.param}: {row.annotation}"
        chars = int((LABEL_W - 10) / 7.6)
        parts.append(
            f'<text class="sg-param" x="0" y="{num(y + CAPTION_H + 11)}">'
            f"<title>{esc(label)}</title>{esc(truncate(row.param, chars))}</text>"
        )
        if row.annotation:
            parts.append(
                f'<text class="sg-annotation" x="0" y="{num(y + CAPTION_H + 25)}">'
                f"<title>{esc(label)}</title>{esc(truncate(row.annotation, int((LABEL_W - 10) / 6.2)))}</text>"
            )
        x = LABEL_W
        line_y = y + CAPTION_H
        families: list[tuple[str, list[GridCell]]] = []
        for cell in row.cells:
            if families and families[-1][0] == cell.family:
                families[-1][1].append(cell)
            else:
                families.append((cell.family, [cell]))
        if not row.cells:
            parts.append(
                f'<text class="sg-empty" x="{num(LABEL_W)}" y="{num(line_y + 11)}">'
                f"{esc(row.note or 'no input classes expected')}</text>"
            )
        for family, cells in families:
            group_w = sum(CELL + 4 + _label_w(c.short) + CELL_GAP for c in cells)
            if x > LABEL_W and x + min(group_w, max_x - LABEL_W) > max_x:
                x = LABEL_W
                line_y += LINE_H + CAPTION_H
            parts.append(f'<text class="sg-family" x="{num(x)}" y="{num(line_y - 4)}">{esc(family)}</text>')
            caption_end = x + len(family) * FAMILY_CHAR_W + GROUP_GAP
            for cell in cells:
                cw = CELL + 4 + _label_w(cell.short)
                if x + cw > max_x and x > LABEL_W:
                    x = LABEL_W
                    line_y += LINE_H + CAPTION_H
                    caption_end = LABEL_W
                parts.append(_cell(cell, x, line_y, cell_url, target, model.missing_reason))
                x += cw + CELL_GAP
            x = max(x + GROUP_GAP - CELL_GAP, caption_end)
        y = line_y + LINE_H + ROW_GAP
        if index < len(model.rows) - 1:
            rule_y = num(y - ROW_GAP / 2)
            parts.append(f'<line class="sg-rule" x1="0" x2="{num(width)}" y1="{rule_y}" y2="{rule_y}"/>')
    height = max(y, 30.0)
    svg = (
        f'<svg class="shape-grid" viewBox="0 0 {num(width)} {num(height)}"'
        f' role="group" aria-label="{attr("Shape grid of " + model.qualname)}">{"".join(parts)}</svg>'
    )
    hollow = model.expected - model.observed
    note = ""
    if hollow:
        noun = "input class" if hollow == 1 else "input classes"
        note = (
            '<p class="sg-note"><svg viewBox="0 0 14 14" class="sw" aria-hidden="true">'
            '<rect class="sg-hollow" x="1" y="1" width="12" height="12" rx="1"/></svg>'
            f" {hollow} {noun} {esc(model.missing_reason)}</p>"
        )
    extra = f'<p class="sg-note">{esc(model.note)}</p>' if model.note else ""
    return Markup(
        f'<div class="shape-grid-block" data-fn="{attr(model.func_key)}">'
        f'<p class="sg-header">{esc(model.header)}</p>'
        f'<div class="sg-scroll">{svg}</div>{note}{extra}'
        f'<div class="sg-detail" id="{attr(target)}" aria-live="polite"></div></div>'
    )


def _calls(samples: int) -> str:
    return f"{samples} sampled call{'s' if samples != 1 else ''}"


def _cell(
    cell: GridCell, x: float, y: float, cell_url: Callable[[GridCell], str], target: str, missing: str
) -> str:
    if cell.observed:
        level = shade_level(cell.samples)
        role = " sg-return" if cell.is_return else ""
        shape = (
            f'<rect class="sg-filled sg-f{level}{role}" x="{num(x)}" y="{num(y)}" width="{num(CELL)}"'
            f' height="{num(CELL)}" rx="2"/>'
        )
        what = ("returned in " if cell.is_return else "observed in ") + _calls(cell.samples)
    else:
        shape = (
            f'<rect class="sg-hollow" x="{num(x + 1)}" y="{num(y + 1)}" width="{num(CELL - 2)}"'
            f' height="{num(CELL - 2)}" rx="1"/>'
        )
        what = missing
    label = f"{cell.name}: {what}"
    return (
        f'<g class="sg-cell{" is-hollow" if not cell.observed else ""}" tabindex="0" role="button"'
        f' aria-label="{attr(label)}" hx-get="{attr(safe_href(cell_url(cell)))}"'
        f' hx-target="#{attr(target)}" hx-swap="innerHTML">'
        f"<title>{esc(label)}</title>{shape}"
        f'<text class="sg-label" x="{num(x + CELL + 4)}" y="{num(y + 11)}">{esc(cell.short)}</text></g>'
    )


def render_strip(cells: Sequence[GridCell], *, label: str = "", missing: str = "never exercised") -> Markup:
    """A one-line strip of small squares (filled = observed, hollow = never exercised); input
    classes only, the return value is never part of it."""
    cells = [c for c in cells if not c.is_return]
    size, gap = 9.0, 3.0
    width = max(1.0, len(cells) * (size + gap))
    items = []
    for i, cell in enumerate(cells):
        x = i * (size + gap)
        if cell.observed:
            items.append(
                f'<rect class="sg-filled sg-f{shade_level(cell.samples)}" x="{num(x + 0.5)}" y="1.5"'
                f' width="{num(size - 1)}" height="{num(size - 1)}" rx="1.5">'
                f"<title>{esc(cell.name)}: observed in {_calls(cell.samples)}</title></rect>"
            )
        else:
            items.append(
                f'<rect class="sg-hollow" x="{num(x + 0.75)}" y="1.75" width="{num(size - 1.5)}"'
                f' height="{num(size - 1.5)}" rx="1"><title>{esc(cell.name)}: {esc(missing)}</title></rect>'
            )
    observed = sum(1 for c in cells if c.observed)
    aria = label or f"{observed} of {len(cells)} input classes observed"
    return Markup(
        f'<svg class="shape-strip" viewBox="0 0 {num(width)} 12" width="{num(width)}" height="12"'
        f' role="img" aria-label="{attr(aria)}">{"".join(items)}</svg>'
    )


MAX_HIDDEN_NAMES = 4


def render_inlay(
    chips: Sequence[tuple[str, str | None, Sequence[GridCell], Sequence[GridCell]]],
    *,
    note: str,
    shapes_url: str,
    missing: str = "never exercised",
) -> Markup:
    """The inlay row under a def line: ``email: str  plain 14 · multi-label-domain 0`` per parameter,
    hollow chips for classes never exercised, a "+K more (names)" chip for classes the row leaves
    out (hollow when one of them was never exercised) and a lazily loaded shape grid (key ``i``)."""
    parts = []
    for param, annotation, cells, hidden in chips:
        label = f"{param}: {annotation}" if annotation else param
        items = []
        for cell in cells:
            cls = "chip" if cell.observed else "chip chip-hollow"
            state = f"observed in {_calls(cell.samples)}" if cell.observed else missing
            items.append(
                f'<span class="{cls}" title="{attr(cell.name + ": " + state)}">{esc(cell.short)}'
                f" <b>{cell.samples}</b></span>"
            )
        if hidden:
            names = [c.short for c in hidden]
            shown_names = ", ".join(names[:MAX_HIDDEN_NAMES]) + ("…" if len(names) > MAX_HIDDEN_NAMES else "")
            gaps = [c.short for c in hidden if not c.observed]
            title = f"Not shown: {', '.join(names)}"
            if gaps:
                title += f" ({len(gaps)} {missing}: {', '.join(gaps)})"
            cls = "chip chip-more chip-hollow" if gaps else "chip chip-more"
            items.append(
                f'<span class="{cls}" title="{attr(title)}">+{len(hidden)} more ({esc(shown_names)})</span>'
            )
        parts.append(f'<span class="inlay-param"><code>{esc(label)}</code>{"".join(items)}</span>')
    if not parts:
        parts.append(f'<span class="inlay-note">{esc(note)}</span>')
    grid = (
        f'<details class="fn-grid" hx-get="{attr(safe_href(shapes_url))}" hx-trigger="toggle once"'
        ' hx-target="find .grid-body"><summary>shape grid <kbd>i</kbd></summary>'
        '<div class="grid-body">Loading…</div></details>'
    )
    return Markup(f'<div class="inlay">{"".join(parts)}{grid}</div>')
