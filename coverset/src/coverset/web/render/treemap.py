"""Squarified treemap of a run: directory → file → function, area = statements, as SVG.

Layout is pure Python (Bruls, Huizing and van Wijk's squarified algorithm). Leaves are functions
and each file's ``<module>`` code; colour is a CSS class per bucket so light and dark themes come
from the stylesheet. Buckets have round edges (0-20-40-60-80-100) and the lowest one is already a
visible step of its ramp. Modes:

``gap`` (default)
    line coverage % minus input-class coverage % on one hue: the darker a leaf, the more of it
    ran without its tests ever passing in whole kinds of input.
``lines``
    line coverage % on the blue ("ran") ramp.
``inputs``
    input-class coverage % on the blue ramp.

In ``inputs`` and ``gap`` a function no test called is hatched (there is nothing to compare), a
called function with no expected input classes is neutral grey, and module-level code is an
outlined tile in the surface colour (it takes no arguments, so it has no input classes). Clicking a
leaf opens the source view at its def line; hovering shows the numbers. The SVG is a ``group`` of
links, each with its own ``aria-label``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from markupsafe import Markup

from coverset.web.render.html import attr, esc, num, safe_href, truncate

__all__ = [
    "MODES",
    "Leaf",
    "Rect",
    "TreeItem",
    "build_tree",
    "layout",
    "leaf_bucket",
    "render_treemap",
    "squarify",
]

Mode = Literal["gap", "lines", "inputs"]
MODES: tuple[Mode, ...] = ("gap", "lines", "inputs")
BUCKETS = 5
HEADER_H = 15.0
PAD = 2.0
MIN_LABEL_W = 46.0
MIN_LABEL_H = 13.0


@dataclass(frozen=True, slots=True)
class Rect:
    """An axis-aligned rectangle."""

    x: float
    y: float
    w: float
    h: float

    @property
    def area(self) -> float:
        """Width times height."""
        return self.w * self.h

    def inset(self, top: float, pad: float) -> Rect:
        """This rectangle shrunk by ``pad`` on each side and ``top`` more at the top."""
        w = max(0.0, self.w - 2 * pad)
        h = max(0.0, self.h - 2 * pad - top)
        return Rect(self.x + pad, self.y + pad + top, w, h)


@dataclass(slots=True)
class TreeItem:
    """A directory, file or leaf of the treemap hierarchy."""

    name: str
    path: str
    kind: Literal["dir", "file", "function"]
    statements: int = 0
    covered: int = 0
    input_cov: float | None = None
    func_key: str | None = None
    def_line: int | None = None
    file: str | None = None
    children: list[TreeItem] = field(default_factory=list)

    @property
    def size(self) -> int:
        """Area weight (statements)."""
        return self.statements


@dataclass(frozen=True, slots=True)
class Leaf:
    """A laid-out treemap item."""

    item: TreeItem
    rect: Rect
    depth: int


def squarify(values: Sequence[float], rect: Rect) -> list[Rect]:
    """Rectangles for ``values`` (any order, all > 0) tiling ``rect``, areas proportional.

    Returns rectangles in the order of ``values``.
    """
    total = float(sum(values))
    if not values or total <= 0 or rect.w <= 0 or rect.h <= 0:
        return [Rect(rect.x, rect.y, 0.0, 0.0) for _ in values]
    scale = rect.area / total
    order = sorted(range(len(values)), key=lambda i: -values[i])
    areas = [values[i] * scale for i in order]
    out: dict[int, Rect] = {}
    x, y, w, h = rect.x, rect.y, rect.w, rect.h
    start = 0
    while start < len(areas):
        short = min(w, h)
        row = [areas[start]]
        end = start + 1
        while end < len(areas) and _worst(row + [areas[end]], short) <= _worst(row, short):
            row.append(areas[end])
            end += 1
        row_sum = sum(row)
        if w >= h:  # lay the row out as a column on the left
            col_w = row_sum / h if h else 0.0
            cy = y
            for k, area in enumerate(row):
                rh = area / col_w if col_w else 0.0
                out[order[start + k]] = Rect(x, cy, col_w, rh)
                cy += rh
            x += col_w
            w = max(0.0, w - col_w)
        else:  # lay it out as a row along the top
            row_h = row_sum / w if w else 0.0
            cx = x
            for k, area in enumerate(row):
                rw = area / row_h if row_h else 0.0
                out[order[start + k]] = Rect(cx, y, rw, row_h)
                cx += rw
            y += row_h
            h = max(0.0, h - row_h)
        start = end
    return [out[i] for i in range(len(values))]


def _worst(row: Sequence[float], short: float) -> float:
    total = sum(row)
    if total <= 0 or short <= 0:
        return float("inf")
    side2 = short * short
    return max(max(side2 * a / (total * total), (total * total) / (side2 * a)) for a in row if a > 0)


def build_tree(nodes: Sequence[object], root: str | None = None) -> TreeItem:
    """A hierarchy from :func:`coverset.facts.queries.treemap` nodes (pre-order list).

    ``root`` narrows to one directory or file path (whole path components).
    """
    root = root.strip("/") if root else None
    top = TreeItem(name=root or "", path=root or "", kind="dir")
    found_root = False
    index: dict[str, TreeItem] = {}
    for node in nodes:
        path: str = node.path  # type: ignore[attr-defined]
        kind = node.kind  # type: ignore[attr-defined]
        file_path, _, qual = path.partition("::")
        item = TreeItem(
            name=qual if kind == "function" else path.rsplit("/", 1)[-1],
            path=path,
            kind=kind,
            statements=int(node.statements),  # type: ignore[attr-defined]
            covered=int(node.covered),  # type: ignore[attr-defined]
            input_cov=node.input_cov,  # type: ignore[attr-defined]
            func_key=node.func_key,  # type: ignore[attr-defined]
            def_line=node.def_line,  # type: ignore[attr-defined]
            file=file_path,
        )
        if root:
            if path == root:
                top, found_root = item, True
                index[path] = item
                continue
            if not (file_path == root or file_path.startswith(root + "/")):
                continue
        parent_path = file_path if kind == "function" else _dirname(path)
        index.get(parent_path, top).children.append(item)
        index[path] = item
    if not found_root:
        top.statements = sum(c.statements for c in top.children)
        top.covered = sum(c.covered for c in top.children)
    return _collapse(top) if not root else top


def _dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _collapse(item: TreeItem) -> TreeItem:
    """Skip single-child directory chains at the top (``src`` → ``src/ledger``)."""
    while item.kind == "dir" and len(item.children) == 1 and item.children[0].kind == "dir":
        item = item.children[0]
    return item


def layout(tree: TreeItem, rect: Rect, *, depth: int = 0) -> list[Leaf]:
    """Every node with its rectangle, parents before children (zero-size items skipped)."""
    out: list[Leaf] = []
    children = [c for c in tree.children if c.size > 0]
    if not children:
        return out
    rects = squarify([c.size for c in children], rect)
    for child, box in zip(children, rects, strict=True):
        out.append(Leaf(child, box, depth))
        if child.children:
            header = HEADER_H if box.h > HEADER_H * 2.5 and box.w > MIN_LABEL_W else 0.0
            inner = box.inset(header, PAD if box.w > 8 and box.h > 8 else 0.0)
            out.extend(layout(child, inner, depth=depth + 1))
    return out


def _line_pct(item: TreeItem) -> float | None:
    return 100.0 * item.covered / item.statements if item.statements else None


MODULE_LEAF = "<module>"


def is_module(item: TreeItem) -> bool:
    """Whether ``item`` is a file's module-level code."""
    return item.kind == "function" and item.func_key is None and item.name == MODULE_LEAF


def leaf_bucket(item: TreeItem, mode: Mode) -> tuple[str, str]:
    """``(css class, hover text)`` of a leaf in ``mode``."""
    line = _line_pct(item)
    inputs = 100.0 * item.input_cov if item.input_cov is not None else None
    called = item.covered > 0
    base = f"{item.statements} statements · lines {_pct(line)}"
    if inputs is not None:
        base += f" · input classes {_pct(inputs)}"
    if mode == "lines":
        return (f"tm-b{_bucket(line)}" if line is not None else "tm-none"), base
    if is_module(item):
        return "tm-module", base + " · module-level code: no input classes"
    if inputs is None:
        if not called:
            return "tm-hatch", base + " · never called by a test"
        return "tm-none", base + " · no input classes expected"
    if mode == "inputs":
        return f"tm-b{_bucket(inputs)}", base
    gap = max(0.0, (line or 0.0) - inputs)
    return f"tm-g{_bucket(gap)}", base + f" · gap {gap:.0f} points"


def _bucket(value: float) -> int:
    """1-5 for 0-20, 20-40, 40-60, 60-80, 80-100 (100 falls in the top bucket)."""
    return max(1, min(BUCKETS, 1 + int(value // (100.0 / BUCKETS))))


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}%"


def render_treemap(
    tree: TreeItem,
    *,
    mode: Mode = "gap",
    width: float = 1200.0,
    height: float = 620.0,
    leaf_url: Callable[[TreeItem], str],
    dir_url: Callable[[TreeItem], str],
    label: str = "",
) -> Markup:
    """The treemap SVG (``role="group"``; every tile is a link with an aria-label and a hover title)."""
    nodes = layout(tree, Rect(0.0, 0.0, width, height))
    parts: list[str] = [
        f'<svg class="treemap mode-{attr(mode)}" viewBox="0 0 {num(width)} {num(height)}"'
        f' role="group" aria-label="{attr(label or "Treemap of statements by function")}">',
        '<defs><pattern id="tm-hatch" width="6" height="6" patternUnits="userSpaceOnUse"'
        ' patternTransform="rotate(45)"><rect class="tm-hatch-bg" width="6" height="6"/>'
        '<line class="tm-hatch-line" x1="0" y1="0" x2="0" y2="6"/></pattern></defs>',
    ]
    for node in nodes:
        item, box = node.item, node.rect
        if box.w <= 0.5 or box.h <= 0.5:
            continue
        geometry = f'x="{num(box.x)}" y="{num(box.y)}" width="{num(box.w)}" height="{num(box.h)}"'
        if item.kind == "function":
            cls, text = leaf_bucket(item, mode)
            title = f"{item.file}::{item.name} · {text}"
            parts.append(
                f'<a href="{attr(safe_href(leaf_url(item)))}" class="tm-leaf-link" aria-label="{attr(title)}">'
                f"<title>{esc(title)}</title>"
                f'<rect class="tm-leaf {cls}" {geometry} rx="2"/>'
            )
            if box.w >= MIN_LABEL_W and box.h >= MIN_LABEL_H:
                chars = max(3, int((box.w - 6) / 6.2))
                parts.append(
                    f'<text class="tm-label" x="{num(box.x + 4)}" y="{num(box.y + 11)}">'
                    f"{esc(truncate(item.name, chars))}</text>"
                )
            parts.append("</a>")
        else:
            cls = "tm-dir" if item.kind == "dir" else "tm-file"
            line = _line_pct(item)
            title = f"{item.path} · {item.statements} statements · lines {_pct(line)}"
            parts.append(
                f'<a href="{attr(safe_href(dir_url(item)))}" class="tm-group-link" aria-label="{attr(title)}">'
                f"<title>{esc(title)}</title>"
                f'<rect class="{cls}" {geometry} rx="3"/>'
            )
            if box.h > HEADER_H * 2.5 and box.w > MIN_LABEL_W:
                chars = max(3, int((box.w - 8) / 6.4))
                parts.append(
                    f'<text class="tm-group-label" x="{num(box.x + 5)}" y="{num(box.y + 12)}">'
                    f"{esc(truncate(item.name, chars))}</text>"
                )
            parts.append("</a>")
    parts.append("</svg>")
    return Markup("".join(parts))


def render_legend(mode: Mode) -> Markup:
    """The colour key for ``mode`` (swatches plus words, so colour never carries meaning alone)."""
    step = 100 // BUCKETS
    prefix = "tm-g" if mode == "gap" else "tm-b"
    label = {
        "gap": "line % minus input-class %",
        "lines": "line coverage",
        "inputs": "input-class coverage",
    }[mode]
    items = [
        f'<li><svg viewBox="0 0 14 14" class="sw" aria-hidden="true"><rect class="{prefix}{i}"'
        f' width="14" height="14" rx="2"/></svg>{(i - 1) * step}–{i * step}</li>'
        for i in range(1, BUCKETS + 1)
    ]
    extra = ""
    if mode != "lines":
        extra = (
            '<li><svg viewBox="0 0 14 14" class="sw" aria-hidden="true"><rect class="tm-hatch"'
            ' width="14" height="14" rx="2"/></svg>never called</li>'
            '<li><svg viewBox="0 0 14 14" class="sw" aria-hidden="true"><rect class="tm-none"'
            ' width="14" height="14" rx="2"/></svg>no input classes</li>'
            '<li><svg viewBox="0 0 14 14" class="sw" aria-hidden="true"><rect class="tm-module"'
            ' x="0.75" y="0.75" width="12.5" height="12.5" rx="2"/></svg>module-level code</li>'
        )
    return Markup(
        f'<div class="legend"><span class="legend-title">{esc(label)}</span>'
        f"<ul>{''.join(items)}{extra}</ul></div>"
    )
