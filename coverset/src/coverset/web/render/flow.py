"""Layered flow SVGs in the flow ribbon's visual grammar, and the test page's data flow.

:func:`render_flow` draws any left-to-right layered flow: columns of nodes (thin bars) joined by
ribbons whose width is their weight and whose shade (``rb-l1`` … ``rb-l4``, the ribbon's steps)
is the caller's choice. An edge may skip a column (a test calling a function directly passes the
"via" column). Every node is a link when it has an ``href`` (``<a>`` with an ``aria-label`` and a
hover ``<title>``), so the SVG is a group of links like the treemap. Labels: first column left of
its bars, last column right of them, middle columns just above their bars (with a halo).

:func:`build_test_flow` turns one test's calls into those columns: the test → in-repo callers the
shape observations name ("via", only when a call came through another function) → the functions it
called → their parameters, labelled with how many input classes this test carried. Ribbon width is
sampled calls (a function the tracer never sampled for this test still gets a hairline); the shade
steps with the classes carried (1, 2, 3–4, 5+), as in the ribbon.

Every interpolated value is escaped (``esc``/``attr``) and every URL passes ``safe_href``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from markupsafe import Markup

from coverset.web.render.html import attr, esc, num, safe_href, truncate

__all__ = ["FlowEdge", "FlowNode", "TestCall", "build_test_flow", "render_flow", "render_test_flow"]

CHAR_W = 6.4
NODE_W = 4.0
NODE_GAP = 26.0
MIN_NODE_H = 3.0
LABEL_ABOVE = 5.0
TOP = 34.0
BOTTOM = 10.0
MIN_EDGE = 1.0


@dataclass(frozen=True, slots=True)
class FlowNode:
    """One bar: ``key`` is unique within its column; ``note`` is short text after the label."""

    key: str
    label: str
    href: str | None = None
    title: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class FlowEdge:
    """A ribbon from ``(column, key)`` to ``(column, key)`` (a later column, possibly skipping one)."""

    source: tuple[int, str]
    target: tuple[int, str]
    weight: float
    level: int = 1
    """Shade step 1-4 (``rb-l1`` … ``rb-l4``)."""
    title: str = ""


@dataclass(slots=True)
class _Box:
    node: FlowNode
    column: int
    order: int
    total: float = 0.0
    y: float = 0.0
    h: float = 0.0
    out_offset: float = 0.0
    in_offset: float = 0.0
    sums: list[float] = field(default_factory=lambda: [0.0, 0.0])


def level(classes: int) -> int:
    """The ribbon's shade step for a number of input classes (1, 2, 3–4, 5+)."""
    return 1 if classes <= 1 else 2 if classes <= 2 else 3 if classes <= 4 else 4


def render_flow(
    columns: Sequence[tuple[str, Sequence[FlowNode]]],
    edges: Sequence[FlowEdge],
    *,
    width: float = 900.0,
    label: str = "Flow",
    legend: Markup | None = None,
    empty: str = "Nothing to draw.",
) -> Markup:
    """The layered flow SVG (an empty-state paragraph when no edge has a weight).

    ``columns`` are ``(heading, nodes)`` left to right; empty columns take no space. Node heights
    use one scale for the whole chart, so a ribbon is as wide at both ends.
    """
    boxes: dict[tuple[int, str], _Box] = {}
    for c, (_heading, nodes) in enumerate(columns):
        for i, node in enumerate(nodes):
            boxes.setdefault((c, node.key), _Box(node, c, i))
    edges = [e for e in edges if e.weight > 0 and e.source in boxes and e.target in boxes]
    edges = [e for e in edges if boxes[e.source].column < boxes[e.target].column]
    if not edges:
        return Markup(f'<p class="empty">{esc(empty)}</p>')
    for edge in edges:
        weight = max(MIN_EDGE, float(edge.weight))
        boxes[edge.source].sums[1] += weight
        boxes[edge.target].sums[0] += weight
    used = sorted({key[0] for key, box in boxes.items() if any(box.sums)})
    for box in boxes.values():
        box.total = max(box.sums)
    per_column = {c: [b for b in boxes.values() if b.column == c and b.total > 0] for c in used}
    for column in per_column.values():
        column.sort(key=lambda b: b.order)
    most = max(len(col) for col in per_column.values())
    body = max(120.0, 30.0 * most)
    scale = min((body - NODE_GAP * (len(col) - 1)) / sum(b.total for b in col) for col in per_column.values())
    scale = max(scale, 0.01)
    bottom = 0.0
    for column in per_column.values():
        y = TOP
        for box in column:
            box.y = y
            box.h = max(MIN_NODE_H, box.total * scale)
            y += box.h + NODE_GAP
        bottom = max(bottom, y - NODE_GAP)
    height = bottom + BOTTOM

    first, last = used[0], used[-1]
    left_w = min(width * 0.3, max(len(b.node.label) for b in per_column[first]) * CHAR_W + 12)
    right_w = min(
        width * 0.28,
        max(len(_with_note(b.node)) for b in per_column[last]) * CHAR_W + 16 if last != first else 0,
    )
    x_first, x_last = left_w, width - right_w - NODE_W
    xs: dict[int, float] = {}
    for i, c in enumerate(used):
        xs[c] = x_first + (x_last - x_first) * (i / (len(used) - 1) if len(used) > 1 else 0.0)
    span = (x_last - x_first) / max(1, len(used) - 1)
    middle_chars = max(6, int((span - 14) / CHAR_W))

    parts: list[str] = []
    for c in used:
        heading = columns[c][0]
        if c == first:
            parts.append(_text("rb-col", xs[c] - 6, 12, heading, "end"))
        else:
            parts.append(_text("rb-col", xs[c], 12, heading, "start"))
    for edge in sorted(
        edges, key=lambda e: (boxes[e.source].order, boxes[e.target].column, boxes[e.target].order)
    ):
        a, b = boxes[edge.source], boxes[edge.target]
        w = max(MIN_EDGE, float(edge.weight)) * scale
        x0, x1 = xs[a.column] + NODE_W, xs[b.column]
        y0a, y1a = a.y + a.out_offset, a.y + a.out_offset + w
        y0b, y1b = b.y + b.in_offset, b.y + b.in_offset + w
        a.out_offset += w
        b.in_offset += w
        xm = (x0 + x1) / 2
        d = (
            f"M{num(x0)},{num(y0a)} C{num(xm)},{num(y0a)} {num(xm)},{num(y0b)} {num(x1)},{num(y0b)}"
            f" L{num(x1)},{num(y1b)} C{num(xm)},{num(y1b)} {num(xm)},{num(y1a)} {num(x0)},{num(y1a)} Z"
        )
        title = edge.title or f"{a.node.label} → {b.node.label}"
        lvl = min(4, max(1, edge.level))
        parts.append(f'<path class="rb-link rb-l{lvl}" d="{d}"><title>{esc(title)}</title></path>')
    for c in used:
        for box in per_column[c]:
            parts.append(
                _node(
                    box,
                    xs[c],
                    first=c == first,
                    last=c == last,
                    left_w=left_w,
                    right_w=right_w,
                    middle_chars=middle_chars,
                )
            )
    legend_html = Markup(legend) if legend is not None else Markup("")
    return Markup(
        f'<div class="flow-block"><svg class="flow-test" viewBox="0 0 {num(width)} {num(height)}"'
        f' role="group" aria-label="{attr(label)}">{"".join(parts)}</svg>{legend_html}</div>'
    )


def _with_note(node: FlowNode) -> str:
    return f"{node.label} · {node.note}" if node.note else node.label


def _text(cls: str, x: float, y: float, text: str, anchor: str = "start") -> str:
    return f'<text class="{cls}" x="{num(x)}" y="{num(y)}" text-anchor="{anchor}">{esc(text)}</text>'


def _node(
    box: _Box, x: float, *, first: bool, last: bool, left_w: float, right_w: float, middle_chars: int
) -> str:
    node = box.node
    title = node.title or _with_note(node)
    rect = (
        f'<rect class="rb-node" x="{num(x)}" y="{num(box.y)}" width="{num(NODE_W)}"'
        f' height="{num(box.h)}" rx="2"/>'
    )
    ty = box.y + box.h / 2 + 4
    if first:
        text = _text("rb-label", x - 6, ty, truncate(node.label, max(6, int((left_w - 12) / CHAR_W))), "end")
    elif last:
        text = _text(
            "rb-label", x + NODE_W + 6, ty, truncate(_with_note(node), max(6, int((right_w - 8) / CHAR_W)))
        )
    else:
        text = _text("rb-label rb-halo", x, box.y - LABEL_ABOVE, truncate(_with_note(node), middle_chars))
    inner = f"<title>{esc(title)}</title>{rect}{text}"
    if node.href:
        return (
            f'<a href="{attr(safe_href(node.href))}" class="fl-link" aria-label="{attr(title)}">{inner}</a>'
        )
    return f'<g class="fl-node">{inner}</g>'


# -- the test page's flow --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TestCall:
    """One function a test called, one parameter of it, and what this test passed in.

    ``via`` is the in-repo caller when the shape observations show the call came through another
    function (None for a direct call from the test). ``param`` is None for a function with no
    sampled parameter (it still gets a node: the test ran its body).
    """

    __test__ = False

    function: FlowNode
    param: str | None = None
    param_href: str | None = None
    samples: int = 0
    classes: int = 0
    via: FlowNode | None = None


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def build_test_flow(
    test: FlowNode, calls: Sequence[TestCall]
) -> tuple[list[tuple[str, list[FlowNode]]], list[FlowEdge]]:
    """The columns and edges of one test's flow (see the module docstring)."""
    vias: dict[str, FlowNode] = {}
    functions: dict[str, FlowNode] = {}
    params: dict[str, FlowNode] = {}
    weights: dict[tuple[tuple[int, str], tuple[int, str]], list[int]] = defaultdict(lambda: [0, 0])
    param_classes: dict[str, int] = defaultdict(int)
    for call in calls:
        functions.setdefault(call.function.key, call.function)
        samples = max(0, call.samples)
        fn_key = (2, call.function.key)
        if call.via is not None:
            vias.setdefault(call.via.key, call.via)
            via_key = (1, call.via.key)
            _add(weights, (0, test.key), via_key, samples, call.classes)
            _add(weights, via_key, fn_key, samples, call.classes)
        else:
            _add(weights, (0, test.key), fn_key, samples, call.classes)
        if call.param is not None:
            pkey = f"{call.function.key}\x00{call.param}"
            param_classes[pkey] = max(param_classes[pkey], call.classes)
            params.setdefault(
                pkey,
                FlowNode(key=pkey, label=call.param, href=call.param_href),
            )
            _add(weights, fn_key, (3, pkey), samples, call.classes)
    param_nodes = []
    for key, node in params.items():
        classes = param_classes[key]
        fn_label = functions[key.split("\x00", 1)[0]].label
        note = _plural(classes, "class", "classes")
        param_nodes.append(
            FlowNode(
                key=key,
                label=node.label,
                href=node.href,
                note=note,
                title=f"{fn_label}({node.label}): this test carried {note}",
            )
        )
    columns: list[tuple[str, list[FlowNode]]] = [
        ("test", [test]),
        ("via", list(vias.values())),
        ("functions it called", list(functions.values())),
        ("parameters · classes this test carried", param_nodes),
    ]
    edges = []
    for (a, b), (samples, classes) in weights.items():
        what = (
            f"{_plural(samples, 'sampled call')}, {_plural(classes, 'input class', 'input classes')}"
            if samples
            else "its body ran; the tracer sampled no call for this test"
        )
        source = test.label if a[0] == 0 else (vias[a[1]].label if a[0] == 1 else functions[a[1]].label)
        target = functions[b[1]].label if b[0] == 2 else vias[b[1]].label if b[0] == 1 else params[b[1]].label
        title = f"{source} → {target}: {what}"
        edges.append(FlowEdge(a, b, max(samples, MIN_EDGE), level(classes), title))
    return columns, edges


def _add(
    weights: dict[tuple[tuple[int, str], tuple[int, str]], list[int]],
    a: tuple[int, str],
    b: tuple[int, str],
    samples: int,
    classes: int,
) -> None:
    slot = weights[(a, b)]
    slot[0] += samples
    slot[1] = max(slot[1], classes)


FLOW_LEGEND = Markup(
    '<p class="rb-legend">Ribbon width: sampled calls (a hairline when the tracer sampled none for '
    "this test). Shade: input classes this test carried "
    '<svg viewBox="0 0 64 10" class="sw-row" aria-hidden="true">'
    + "".join(
        f'<rect class="rb-l{i}" x="{(i - 1) * 16}" width="14" height="10" rx="2"/>' for i in range(1, 5)
    )
    + "</svg> 1, 2, 3–4, 5+. Every bar is a link.</p>"
)


def render_test_flow(test: FlowNode, calls: Sequence[TestCall], *, width: float = 1100.0) -> Markup:
    """One test's data-flow SVG: test → via → functions → parameters."""
    columns, edges = build_test_flow(test, calls)
    return render_flow(
        columns,
        edges,
        width=width,
        label=f"Data flow of {test.label}: functions it called and the parameters it passed",
        legend=FLOW_LEGEND,
        empty="This test called no measured function in this run.",
    )
