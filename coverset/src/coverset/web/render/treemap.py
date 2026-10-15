"""Treemap of a run: directory → file → function, area = statements, as SVG.

Layout is pure Python, in two variants:

``ordered`` (default)
    an ordered binary split: siblings keep a fixed order (directories and files by path, a file's
    functions in source order, path as the tie-break); the list is cut where the weight is closest
    to half and the rectangle is cut across its longer side in that proportion, recursively. A
    function that grows by a few statements moves the cuts it sits under by a little and nothing
    else: the order never changes, so two baselines (or a PR run and its baseline) read as the
    same picture. Tiles are a little less square than squarified ones.
``squarified``
    Bruls, Huizing and van Wijk's squarified algorithm: squarer tiles, but sorted by size, so a
    small change in sizes can swap tiles across the map (``?layout=squarified`` on the map page).
    The stability of both, measured with :func:`centre_moves`, is in docs/ui.md.

Module-level code (each file's ``<module>`` leaf: imports, constants, ``def``/``class`` lines) runs
when the module is imported, so it reads as covered whatever the tests do and inflates the covered
area. :func:`weigh_modules` sets how it shows (``view.map_module_weight``): ``full`` (its
statements), ``thin`` (default: at most 10% of the file's function statements, at least 1, drawn
dashed and listed in the legend as "module-level code shown reduced") or ``hidden`` (left out; the
file's tooltip still reports it). :func:`anchor_to` lays a PR run out in its baseline's frame: every
tile and container the baseline has keeps the baseline's area (a new function shares its file's),
so the same rectangles change colour.

Leaves are functions and each file's ``<module>`` code; colour is a CSS class per bucket so light
and dark themes come from the stylesheet. Buckets have round edges (0-20-40-60-80-100) and the
lowest one is already a visible step of its ramp. Modes:

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
links, each with its own ``aria-label`` and the same text in ``data-info``, which the pages' scripts
copy into the status line above the map (``<p class="tm-status" data-tm-status>`` in the
templates, "hover a tile" until then) on hover and keyboard focus.
"""

from __future__ import annotations

import dataclasses
import math
from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import accumulate
from typing import Any, Literal

from markupsafe import Markup

from coverset.web.render.html import attr, esc, num, safe_href, truncate

__all__ = [
    "ALGORITHMS",
    "MAP_COOKIE",
    "MODES",
    "MODULE_WEIGHTS",
    "MODULE_WHY",
    "Leaf",
    "Rect",
    "TreeItem",
    "anchor_to",
    "build_tree",
    "centre_moves",
    "explicit_view_prefs",
    "layout",
    "leaf_bucket",
    "ordered_split",
    "reader_map_choice",
    "render_legend",
    "render_treemap",
    "resolve_view",
    "squarify",
    "weigh_modules",
]

Mode = Literal["gap", "lines", "inputs"]
MODES: tuple[Mode, ...] = ("gap", "lines", "inputs")
ModuleWeight = Literal["full", "thin", "hidden"]
MODULE_WEIGHTS: tuple[ModuleWeight, ...] = ("full", "thin", "hidden")
"""How module-level code shows (``view.map_module_weight``); ``thin`` is the default."""
Algorithm = Literal["ordered", "squarified"]
ALGORITHMS: tuple[Algorithm, ...] = ("ordered", "squarified")
THIN_SHARE = 0.10
"""A ``thin`` module leaf is at most this share of its file's function statements."""
MODULE_WHY = (
    "Imports, constants and def/class lines run when a module is imported, so module-level code "
    "reads as covered whatever the tests do and inflates the covered area."
)
"""Why ``thin`` is the default: the legend's hover text and the map page's note."""
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

    @property
    def centre(self) -> tuple[float, float]:
        """The middle point."""
        return self.x + self.w / 2.0, self.y + self.h / 2.0

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
    weight: float | None = None
    """Area weight when it is not the statements (a reduced module leaf, a parent of one, a tile
    laid out in a baseline's frame); None means the statements."""
    note: str = ""
    """Appended to the hover text (a file's hidden module-level code, a reduced module leaf)."""
    reduced: bool = False
    """A module leaf drawn smaller than its statements (``thin``): dashed outline."""

    @property
    def size(self) -> float:
        """Area weight: :attr:`weight` when set, else the statements."""
        return self.statements if self.weight is None else self.weight


@dataclass(frozen=True, slots=True)
class Leaf:
    """A laid-out treemap item."""

    item: TreeItem
    rect: Rect
    depth: int


# -- layouts ------------------------------------------------------------------------------------


def _zero(values: Sequence[float], rect: Rect) -> list[Rect] | None:
    if not values or float(sum(values)) <= 0 or rect.w <= 0 or rect.h <= 0:
        return [Rect(rect.x, rect.y, 0.0, 0.0) for _ in values]
    return None


def squarify(values: Sequence[float], rect: Rect) -> list[Rect]:
    """Rectangles for ``values`` (any order, all > 0) tiling ``rect``, areas proportional.

    Returns rectangles in the order of ``values``. Tiles are placed largest first (ties by input
    position), which makes them square but lets a small change of sizes reorder the whole map.
    """
    empty = _zero(values, rect)
    if empty is not None:
        return empty
    total = float(sum(values))
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


def ordered_split(values: Sequence[float], rect: Rect) -> list[Rect]:
    """Rectangles for ``values`` (all > 0) tiling ``rect`` in the given order: an ordered binary
    split. The list is cut where its running total is closest to half (the earlier cut on a tie)
    and ``rect`` across its longer side in proportion, then each half the same way.

    Returns rectangles in the order of ``values``. A value that changes a little moves the cuts
    above it by a little; an item crosses a cut only when the totals on both sides nearly balance.
    """
    empty = _zero(values, rect)
    if empty is not None:
        return empty
    prefix = [0.0, *accumulate(float(v) for v in values)]
    out: list[Rect] = [rect] * len(values)
    stack = [(0, len(values), rect)]
    while stack:
        lo, hi, box = stack.pop()
        if hi - lo == 1:
            out[lo] = box
            continue
        total = prefix[hi] - prefix[lo]
        half = prefix[lo] + total / 2.0
        k = bisect_left(prefix, half, lo + 1, hi)
        cut = min((c for c in (k - 1, k) if lo < c < hi), key=lambda c: (abs(prefix[c] - half), c))
        share = (prefix[cut] - prefix[lo]) / total if total > 0 else 0.5
        if box.w >= box.h:
            first = Rect(box.x, box.y, box.w * share, box.h)
            second = Rect(box.x + first.w, box.y, box.w - first.w, box.h)
        else:
            first = Rect(box.x, box.y, box.w, box.h * share)
            second = Rect(box.x, box.y + first.h, box.w, box.h - first.h)
        stack.append((cut, hi, second))
        stack.append((lo, cut, first))
    return out


def _order_key(item: TreeItem) -> tuple[int, int, str]:
    """Siblings' fixed order in the ordered layout: directories and files by path, a file's
    functions in source order (module-level code first, then by ``def`` line), path as the
    tie-break."""
    if item.kind == "function":
        return (0, -1 if is_module(item) else (item.def_line or 0), item.path)
    return (0, 0, item.path)


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


def layout(tree: TreeItem, rect: Rect, *, depth: int = 0, algorithm: Algorithm = "ordered") -> list[Leaf]:
    """Every node with its rectangle, parents before children (zero-size items skipped).

    ``ordered`` (default) keeps siblings in :func:`_order_key` order (:func:`ordered_split`);
    ``squarified`` sorts them by size (:func:`squarify`)."""
    out: list[Leaf] = []
    children = [c for c in tree.children if c.size > 0]
    if not children:
        return out
    if algorithm == "squarified":
        rects = squarify([c.size for c in children], rect)
    else:
        children.sort(key=_order_key)
        rects = ordered_split([c.size for c in children], rect)
    for child, box in zip(children, rects, strict=True):
        out.append(Leaf(child, box, depth))
        if child.children:
            header = HEADER_H if box.h > HEADER_H * 2.5 and box.w > MIN_LABEL_W else 0.0
            inner = box.inset(header, PAD if box.w > 8 and box.h > 8 else 0.0)
            out.extend(layout(child, inner, depth=depth + 1, algorithm=algorithm))
    return out


# -- module weight and the baseline frame ------------------------------------------------------

MODULE_LEAF = "<module>"


def is_module(item: TreeItem) -> bool:
    """Whether ``item`` is a file's module-level code."""
    return item.kind == "function" and item.func_key is None and item.name == MODULE_LEAF


def _module_note(module: TreeItem) -> str:
    pct = _pct(_line_pct(module))
    return f"module-level code: {module.statements} statements, {pct} ran (hidden from the map)"


def weigh_modules(tree: TreeItem, weight: ModuleWeight = "thin") -> TreeItem:
    """``tree`` with its module-level leaves weighed for ``weight`` (a new tree; ``tree`` is not
    changed): ``full`` keeps their statements; ``thin`` gives each the smaller of its statements and
    10% of its file's function statements (at least 1) and marks it ``reduced`` when that is less;
    ``hidden`` drops them and notes their coverage on the file. Parents weigh the sum of their
    children, so a file of nothing but module-level code disappears when they are hidden. Applying
    it twice gives the same tree."""
    if weight == "full":
        return tree

    def visit(item: TreeItem) -> TreeItem:
        if not item.children:
            return item
        kids = [visit(c) for c in item.children]
        note = item.note
        if item.kind == "file":
            modules = [k for k in kids if is_module(k)]
            function_size = sum(k.size for k in kids if not is_module(k))
            if weight == "hidden":
                kids = [k for k in kids if not is_module(k)]
                if modules and not note:
                    note = _module_note(modules[0])
            else:
                cap = max(1.0, THIN_SHARE * function_size)
                kids = [
                    dataclasses.replace(
                        k,
                        weight=min(float(k.statements), cap),
                        reduced=k.statements > cap,
                        note=(
                            f"shown reduced: drawn as {min(float(k.statements), cap):.0f} of "
                            f"{k.statements} statements"
                        )
                        if k.statements > cap
                        else "",
                    )
                    if is_module(k)
                    else k
                    for k in kids
                ]
        return dataclasses.replace(item, children=kids, weight=float(sum(k.size for k in kids)), note=note)

    return visit(tree)


def anchor_to(tree: TreeItem, frame: TreeItem) -> tuple[TreeItem, bool]:
    """``tree`` laid out in ``frame``'s sizes: every directory, file and function that ``frame``
    has (by path) takes its size there, whatever its children add up to now; what ``frame`` lacks
    keeps its own size. A function the PR adds therefore shares its file's baseline area, and a
    tile moves only inside a file or directory whose contents changed: with the ordered layout a PR
    run gets its baseline's rectangles, recoloured. Returns the new tree and whether anything
    differs from ``tree``'s own sizes (the page then says "layout from baseline <sha>"). Both trees
    should be weighed the same way first (:func:`weigh_modules`)."""
    sizes: dict[str, float] = {c.path: c.size for c in _walk(frame)}
    differs = {c.path for c in _walk(tree)} != set(sizes)  # added or removed functions or files

    def visit(item: TreeItem) -> TreeItem:
        nonlocal differs
        kids = [visit(c) for c in item.children]
        own = float(sum(k.size for k in kids)) if kids else item.size
        size = sizes.get(item.path)
        if size is None or size <= 0:
            size = own  # new since the frame (or empty there): its own size
        elif abs(size - item.size) > 1e-9:
            differs = True
        if not kids:
            return item if size == item.size else dataclasses.replace(item, weight=size)
        return dataclasses.replace(item, children=kids, weight=size)

    return visit(tree), differs


def _walk(item: TreeItem) -> Iterable[TreeItem]:
    for child in item.children:
        yield child
        yield from _walk(child)


def centre_moves(
    before: Sequence[Leaf], after: Sequence[Leaf], width: float, height: float, *, threshold: float = 0.05
) -> tuple[int, int]:
    """``(moved, compared)``: of the function leaves laid out in both ``before`` and ``after`` (by
    path), how many have a centre that moved more than ``threshold`` of the map, measured in
    coordinates scaled to the map's width and height (so 5% is a twentieth of the way across in
    either direction, combined as a distance). The stability number of docs/ui.md."""
    old = {leaf.item.path: leaf.rect.centre for leaf in before if leaf.item.kind == "function"}
    moved = compared = 0
    for leaf in after:
        if leaf.item.kind != "function" or leaf.item.path not in old:
            continue
        (x0, y0), (x1, y1) = old[leaf.item.path], leaf.rect.centre
        compared += 1
        if math.hypot((x1 - x0) / width, (y1 - y0) / height) > threshold:
            moved += 1
    return moved, compared


VIEW_DEFAULTS: dict[str, str] = {
    "view.highlight": "all",
    "view.map_mode": "gap",
    "view.map_module_weight": "thin",
    "view.theme": "auto",
}
"""The built-in defaults of the four agreed view keys (the settings catalogue has the same)."""


def explicit_view_prefs(prefs: object) -> dict[str, Any]:
    """The view preferences somebody chose, from the settings layer's resolved ``view_prefs``:
    with a ``sources`` mapping (key → the scope it came from) the keys whose source is not
    ``default``; without one, the keys whose value differs from :data:`VIEW_DEFAULTS` (a value at
    its default may be unset, and then the reader's own browser choice comes first). Anything that
    is not a mapping gives ``{}``."""
    if not isinstance(prefs, Mapping):
        return {}
    sources = getattr(prefs, "sources", None)
    if isinstance(sources, Mapping):
        return {k: v for k, v in prefs.items() if sources.get(k, "default") != "default"}
    return {k: v for k, v in prefs.items() if VIEW_DEFAULTS.get(str(k)) != v}


MAP_COOKIE = "coverset-map"
"""The cookie app.js writes when a reader picks a map mode or module weight (``<mode>.<modules>``,
either part may be empty). The map is drawn on the server, so the reader's own choice has to reach
the server on the first request: from localStorage it would cost a second, full render (a reload),
and the overview's Gap thumbnail would never see it."""


def reader_map_choice(cookie: str | None) -> dict[str, str]:
    """The reader's own map choice from the :data:`MAP_COOKIE` value: ``{"map_mode": …,
    "map_module_weight": …}`` with only the parts that are valid choices (anything else, a missing
    or tampered cookie included, is left out and the page falls back to its default)."""
    if not cookie or len(cookie) > 40:
        return {}
    mode, _, weight = cookie.partition(".")
    found: dict[str, str] = {}
    if mode in MODES:
        found["map_mode"] = mode
    if weight in MODULE_WEIGHTS:
        found["map_module_weight"] = weight
    return found


def resolve_view(
    key: str,
    query: str | None,
    prefs: Mapping[str, Any] | None,
    choices: Sequence[str],
    default: str,
    reader: str | None = None,
) -> tuple[str, str]:
    """``(value, source)`` of one view setting: the query parameter when it is one of ``choices``,
    else the scoped settings' ``view.<key>`` (``prefs``, the ``view_prefs`` the settings resolve:
    user > repo > org > instance), else the reader's own choice in this browser when the server can
    see it (``reader``: the map's :data:`MAP_COOKIE`), else ``default``. ``source`` is ``query``,
    ``prefs``, ``reader`` or ``default``: on ``default`` the page's script may still apply the
    reader's own choice from localStorage (the precedence is query > settings > the reader's
    browser > default)."""
    if query in choices:
        return str(query), "query"
    if prefs:
        found = prefs.get(f"view.{key}", prefs.get(key))
        if found in choices:
            return str(found), "prefs"
    if reader in choices:
        return str(reader), "reader"
    return default, "default"


# -- colours and the SVG -----------------------------------------------------------------------


def _line_pct(item: TreeItem) -> float | None:
    return 100.0 * item.covered / item.statements if item.statements else None


def leaf_bucket(item: TreeItem, mode: Mode) -> tuple[str, str]:
    """``(css class, hover text)`` of a leaf in ``mode``."""
    line = _line_pct(item)
    inputs = 100.0 * item.input_cov if item.input_cov is not None else None
    called = item.covered > 0
    base = f"{item.statements} statements · lines {_pct(line)}"
    if inputs is not None:
        base += f" · input classes {_pct(inputs)}"
    reduced = " tm-reduced" if item.reduced else ""
    if mode == "lines":
        if inputs is None and not called and not is_module(item):
            base += " · never called by a test"
        if is_module(item):
            base += " · module-level code"
        return (f"tm-b{_bucket(line)}" if line is not None else "tm-none") + reduced, base
    if is_module(item):
        return "tm-module" + reduced, base + " · module-level code: no input classes"
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
    module_weight: ModuleWeight = "thin",
    algorithm: Algorithm = "ordered",
) -> Markup:
    """The treemap SVG (``role="group"``; every tile is a link with an aria-label and a hover title).

    ``tree`` is weighed with :func:`weigh_modules` (``module_weight``) unless that was done already:
    a weighed tree (by :func:`weigh_modules` or :func:`anchor_to`) has a weight on its root, and an
    anchored one must keep its frame's sizes, which weighing again would re-sum."""
    if tree.weight is None:
        tree = weigh_modules(tree, module_weight)
    nodes = layout(tree, Rect(0.0, 0.0, width, height), algorithm=algorithm)
    parts: list[str] = [
        f'<svg class="treemap mode-{attr(mode)}" viewBox="0 0 {num(width)} {num(height)}"'
        f' data-modules="{attr(module_weight)}" data-layout="{attr(algorithm)}"'
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
            title = f"{item.file}::{item.name} · {text}" + (f" · {item.note}" if item.note else "")
            parts.append(
                f'<a href="{attr(safe_href(leaf_url(item)))}" class="tm-leaf-link" aria-label="{attr(title)}"'
                f' data-info="{attr(title)}">'
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
            if item.note:
                title += f" · {item.note}"
            parts.append(
                f'<a href="{attr(safe_href(dir_url(item)))}" class="tm-group-link" aria-label="{attr(title)}"'
                f' data-info="{attr(title)}">'
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


def _swatch(cls: str, *, outline: bool = False) -> str:
    geometry = 'x="0.75" y="0.75" width="12.5" height="12.5"' if outline else 'width="14" height="14"'
    return f'<svg viewBox="0 0 14 14" class="sw" aria-hidden="true"><rect class="{cls}" {geometry} rx="2"/></svg>'


def render_legend(mode: Mode, module_weight: ModuleWeight = "thin") -> Markup:
    """The colour key for ``mode`` (swatches plus words, so colour never carries meaning alone),
    with how module-level code shows under ``module_weight`` and why (its hover text)."""
    step = 100 // BUCKETS
    prefix = "tm-g" if mode == "gap" else "tm-b"
    label = {
        "gap": "line % minus input-class %",
        "lines": "line coverage",
        "inputs": "input-class coverage",
    }[mode]
    items = [f"<li>{_swatch(f'{prefix}{i}')}{(i - 1) * step}–{i * step}</li>" for i in range(1, BUCKETS + 1)]
    extra = ""
    if mode != "lines":
        extra = f"<li>{_swatch('tm-hatch')}never called</li><li>{_swatch('tm-none')}no input classes</li>"
    why = attr(MODULE_WHY)
    if module_weight == "hidden":
        extra += (
            f'<li class="lg-module" title="{why}" data-module-weight="hidden">module-level code hidden</li>'
        )
    elif module_weight == "thin":
        cls = "tm-module tm-reduced" if mode != "lines" else "tm-b3 tm-reduced"
        extra += (
            f'<li class="lg-module" title="{why}" data-module-weight="thin">'
            f"{_swatch(cls, outline=True)}module-level code shown reduced</li>"
        )
    elif mode != "lines":
        extra += (
            f'<li class="lg-module" title="{why}" data-module-weight="full">'
            f"{_swatch('tm-module', outline=True)}module-level code</li>"
        )
    return Markup(
        f'<div class="legend"><span class="legend-title">{esc(label)}</span>'
        f"<ul>{''.join(items)}{extra}</ul></div>"
    )
