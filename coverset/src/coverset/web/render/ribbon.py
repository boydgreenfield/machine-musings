"""The flow ribbon: which test files reach a function through which callers, into which parameters.

Three columns (test files → in-repo callers → parameters) built from ``call_shapes``: a test that
calls the function directly is the caller "called from the test"; a call from another in-repo
function names that function. Ribbon width is the number of sampled calls; ribbon colour steps with
the number of distinct input classes the calls carried (more classes, darker), and every ribbon has
a hover title with both numbers. Columns keep their :data:`MAX_NODES` largest nodes and fold the
rest into "other". Node bars are thin; test-file labels sit left of their bar, caller labels just
above theirs (clear of the ribbons that leave it) and parameter labels to the right. The SVG scales
to its container (``width`` is its coordinate width, sized to the column it sits in).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from markupsafe import Markup

from coverset.web.render.html import attr, esc, num, truncate

__all__ = ["Flow", "render_ribbon"]

MAX_NODES = 10
CHAR_W = 6.4
NODE_W = 4.0
NODE_GAP = 22.0
LABEL_ABOVE = 5.0
OTHER = "other"


@dataclass(frozen=True, slots=True)
class Flow:
    """Sampled calls from one test file, through one caller, into one parameter."""

    source: str
    caller: str
    param: str
    samples: int
    classes: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class _Node:
    name: str
    column: int
    total: int = 0
    y: float = 0.0
    h: float = 0.0
    out_offset: float = 0.0
    in_offset: float = 0.0


def _fold(flows: Sequence[Flow]) -> list[Flow]:
    """Keep the largest nodes per column; fold the rest into ``other``."""
    keep: list[set[str]] = []
    for pick in (lambda f: f.source, lambda f: f.caller, lambda f: f.param):
        totals: dict[str, int] = defaultdict(int)
        for flow in flows:
            totals[pick(flow)] += flow.samples
        ranked = sorted(totals, key=lambda k: (-totals[k], k))
        keep.append(set(ranked[:MAX_NODES]) if len(ranked) > MAX_NODES else set(ranked))
    folded: dict[tuple[str, str, str], tuple[int, set[str]]] = {}
    for flow in flows:
        key = (
            flow.source if flow.source in keep[0] else OTHER,
            flow.caller if flow.caller in keep[1] else OTHER,
            flow.param if flow.param in keep[2] else OTHER,
        )
        samples, classes = folded.get(key, (0, set()))
        folded[key] = (samples + flow.samples, classes | set(flow.classes))
    return [Flow(s, c, p, n, frozenset(k)) for (s, c, p), (n, k) in folded.items()]


def _level(classes: int) -> int:
    return 1 if classes <= 1 else 2 if classes <= 2 else 3 if classes <= 4 else 4


def render_ribbon(flows: Sequence[Flow], *, width: float = 900.0, height: float | None = None) -> Markup:
    """The ribbon SVG (an empty-state paragraph when there are no flows)."""
    flows = [f for f in flows if f.samples > 0]
    if not flows:
        return Markup('<p class="empty">No sampled calls recorded for this function.</p>')
    flows = _fold(flows)
    nodes: dict[tuple[int, str], _Node] = {}
    for flow in flows:
        for column, name in enumerate((flow.source, flow.caller, flow.param)):
            node = nodes.setdefault((column, name), _Node(name, column))
            node.total += flow.samples
    columns = [
        sorted((n for n in nodes.values() if n.column == c), key=lambda n: (-n.total, n.name))
        for c in range(3)
    ]
    most = max(len(c) for c in columns)
    height = height or max(150.0, 44.0 * most + 40.0)
    total = sum(f.samples for f in flows)
    left_w = min(width * 0.36, max(len(n.name) for n in columns[0]) * CHAR_W + 12)
    right_w = min(width * 0.25, max(len("parameters"), *(len(n.name) for n in columns[2])) * CHAR_W + 16)
    x0, x2 = left_w, width - right_w - NODE_W
    xs = (x0, x0 + (x2 - x0) * 0.5, x2)
    label_chars = {
        1: max(6, int((x2 - xs[1] - 12) / CHAR_W)),
        2: max(6, int((right_w - 8) / CHAR_W)),
    }
    top = 34.0
    for column in columns:
        usable = height - top - 8.0 - NODE_GAP * (len(column) - 1)
        y = top
        for node in column:
            node.y = y
            node.h = max(2.0, usable * node.total / total)
            y += node.h + NODE_GAP
    parts: list[str] = []
    titles = (("test files", xs[0] - 6, "end"), ("callers", xs[1], "start"), ("parameters", x2, "start"))
    for title, tx, anchor in titles:
        parts.append(f'<text class="rb-col" x="{num(tx)}" y="12" text-anchor="{anchor}">{esc(title)}</text>')
    links: list[tuple[tuple[int, str], tuple[int, str], int, set[str]]] = []
    agg: dict[tuple[tuple[int, str], tuple[int, str]], tuple[int, set[str]]] = {}
    for flow in sorted(flows, key=lambda f: (f.source, f.caller, f.param)):
        for a, b in (((0, flow.source), (1, flow.caller)), ((1, flow.caller), (2, flow.param))):
            n, k = agg.get((a, b), (0, set()))
            agg[(a, b)] = (n + flow.samples, k | set(flow.classes))
    for (a, b), (n, k) in agg.items():
        links.append((a, b, n, k))
    order = {key: i for i, key in enumerate((n.column, n.name) for col in columns for n in col)}
    links.sort(key=lambda link: (order[link[0]], order[link[1]]))
    for a_key, b_key, samples, classes in links:
        a, b = nodes[a_key], nodes[b_key]
        w = (b.h if b.total else 0.0) * samples / b.total if b.total else 0.0
        wa = a.h * samples / a.total if a.total else 0.0
        x0 = xs[a.column] + NODE_W
        x1 = xs[b.column]
        y0a, y1a = a.y + a.out_offset, a.y + a.out_offset + wa
        y0b, y1b = b.y + b.in_offset, b.y + b.in_offset + w
        a.out_offset += wa
        b.in_offset += w
        xm = (x0 + x1) / 2
        d = (
            f"M{num(x0)},{num(y0a)} C{num(xm)},{num(y0a)} {num(xm)},{num(y0b)} {num(x1)},{num(y0b)}"
            f" L{num(x1)},{num(y1b)} C{num(xm)},{num(y1b)} {num(xm)},{num(y1a)} {num(x0)},{num(y1a)} Z"
        )
        noun = "class" if len(classes) == 1 else "classes"
        title = f"{a.name} → {b.name}: {samples} sampled calls, {len(classes)} input {noun}"
        parts.append(
            f'<path class="rb-link rb-l{_level(len(classes))}" d="{d}"><title>{esc(title)}</title></path>'
        )
    for column in columns:
        for node in column:
            x = xs[node.column]
            parts.append(
                f'<rect class="rb-node" x="{num(x)}" y="{num(node.y)}" width="{num(NODE_W)}"'
                f' height="{num(node.h)}" rx="2"><title>{esc(node.name)}: {node.total} sampled calls</title></rect>'
            )
            ty = node.y + node.h / 2 + 4
            if node.column == 0:
                parts.append(
                    f'<text class="rb-label" x="{num(x - 6)}" y="{num(ty)}" text-anchor="end">'
                    f"{esc(truncate(node.name, max(6, int((left_w - 12) / CHAR_W))))}</text>"
                )
            elif node.column == 1:
                parts.append(
                    f'<text class="rb-label rb-halo" x="{num(x)}" y="{num(node.y - LABEL_ABOVE)}">'
                    f"{esc(truncate(node.name, label_chars[1]))}</text>"
                )
            else:
                parts.append(
                    f'<text class="rb-label" x="{num(x + NODE_W + 6)}" y="{num(ty)}">'
                    f"{esc(truncate(node.name, label_chars[2]))}</text>"
                )
    legend = (
        '<p class="rb-legend">Ribbon width: sampled calls. Shade: input classes carried '
        '<svg viewBox="0 0 64 10" class="sw-row" aria-hidden="true">'
        + "".join(
            f'<rect class="rb-l{i}" x="{(i - 1) * 16}" width="14" height="10" rx="2"/>' for i in range(1, 5)
        )
        + "</svg> 1, 2, 3–4, 5+.</p>"
    )
    return Markup(
        f'<div class="ribbon-block"><svg class="flow-ribbon" viewBox="0 0 {num(width)} {num(height)}"'
        f' role="img" aria-label="{attr("Flow from test files through callers to parameters")}">'
        f"{''.join(parts)}</svg>{legend}</div>"
    )
