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
sampled calls: a call passes every parameter at once, so a ribbon into a function (or a "via") is the
most samples any one parameter got, not their sum, and a ribbon into a parameter is that parameter's
samples (a function the tracer never sampled for this test still gets a hairline); the shade steps
with the classes carried (1, 2, 3–4, 5+), as in the ribbon. Only the :data:`MAX_FLOW_FUNCTIONS`
functions with the most sampled calls get a bar (the rest share one "+N more" bar) and only
:data:`MAX_FLOW_PARAMS` parameters per function, so the SVG stays a readable size.

Every interpolated value is escaped (``esc``/``attr``) after its bidirectional controls are made
visible (:func:`~coverset.report.output.mark_bidi`), and every URL passes ``safe_href``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from markupsafe import Markup

from coverset.report.output import mark_bidi
from coverset.web.render.html import attr as _attr
from coverset.web.render.html import esc as _esc
from coverset.web.render.html import num, safe_href, truncate

__all__ = ["FlowEdge", "FlowNode", "TestCall", "build_test_flow", "render_flow", "render_test_flow"]

CHAR_W = 6.4
NODE_W = 4.0
NODE_GAP = 26.0
MIN_NODE_H = 3.0
LABEL_ABOVE = 5.0
TOP = 34.0
BOTTOM = 10.0
MIN_EDGE = 1.0
MAX_FLOW_FUNCTIONS = 40
"""A test's flow draws at most this many functions (by sampled calls); the rest share one bar."""
MAX_FLOW_PARAMS = 12
"""A function's bar fans out to at most this many parameters (by sampled calls)."""
MORE_KEY = "\x00more"


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


def esc(text: object) -> Markup:
    """Escaped text with bidirectional controls shown as ``<U+202E>`` markers."""
    return _esc(mark_bidi("" if text is None else str(text)))


def attr(text: object) -> Markup:
    """An escaped attribute value with bidirectional controls shown as markers."""
    return _attr(mark_bidi("" if text is None else str(text)))


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
    test: FlowNode,
    calls: Sequence[TestCall],
    *,
    max_functions: int = MAX_FLOW_FUNCTIONS,
    max_params: int = MAX_FLOW_PARAMS,
) -> tuple[list[tuple[str, list[FlowNode]]], list[FlowEdge]]:
    """The columns and edges of one test's flow (see the module docstring)."""
    functions: dict[str, FlowNode] = {}
    # (via key or None, function key) -> param (or None) -> [samples, classes]
    per_call: dict[tuple[str | None, str], dict[str | None, list[int]]] = defaultdict(dict)
    via_nodes: dict[str, FlowNode] = {}
    param_href: dict[tuple[str, str], str | None] = {}
    for call in calls:
        functions.setdefault(call.function.key, call.function)
        via = call.via.key if call.via is not None else None
        if call.via is not None:
            via_nodes.setdefault(call.via.key, call.via)
        slot = per_call[(via, call.function.key)].setdefault(call.param, [0, 0])
        slot[0] += max(0, call.samples)
        slot[1] = max(slot[1], call.classes)
        if call.param is not None:
            param_href.setdefault((call.function.key, call.param), call.param_href)

    # one call carries every parameter: a ribbon into a function is its busiest parameter's samples
    into: dict[tuple[str | None, str], tuple[int, int]] = {
        key: (max(v[0] for v in by_param.values()), max(v[1] for v in by_param.values()))
        for key, by_param in per_call.items()
    }
    totals: dict[str, int] = defaultdict(int)
    for (_via, fn), (samples, _classes) in into.items():
        totals[fn] += samples
    order = {key: i for i, key in enumerate(functions)}
    ranked = sorted(functions, key=lambda k: (-totals[k], order[k]))
    kept = set(ranked[:max_functions])
    hidden = [k for k in ranked if k not in kept]

    vias: dict[str, FlowNode] = {}
    weights: dict[tuple[tuple[int, str], tuple[int, str]], list[int]] = defaultdict(lambda: [0, 0])
    params: dict[str, list[int]] = {}
    for (via, fn), (samples, classes) in into.items():
        if fn not in kept:
            continue
        fn_key = (2, fn)
        if via is not None:
            vias.setdefault(via, via_nodes[via])
            _add(weights, (0, test.key), (1, via), samples, classes)
            _add(weights, (1, via), fn_key, samples, classes)
        else:
            _add(weights, (0, test.key), fn_key, samples, classes)
        for param, (p_samples, p_classes) in per_call[(via, fn)].items():
            if param is None:
                continue
            slot = params.setdefault(f"{fn}\x00{param}", [0, 0])
            slot[0] += p_samples
            slot[1] = max(slot[1], p_classes)
    if hidden:
        more_samples = sum(totals[k] for k in hidden)
        _add(weights, (0, test.key), (2, MORE_KEY), more_samples, 0)

    param_nodes: list[FlowNode] = []
    by_function: dict[str, list[tuple[str, list[int]]]] = defaultdict(list)
    for key, slot in params.items():
        by_function[key.split("\x00", 1)[0]].append((key, slot))
    for fn in functions:
        rows = sorted(by_function.get(fn, ()), key=lambda kv: -kv[1][0])
        for key, (samples, classes) in rows[:max_params]:
            name = key.split("\x00", 1)[1]
            note = _plural(classes, "class", "classes")
            param_nodes.append(
                FlowNode(
                    key=key,
                    label=name,
                    href=param_href.get((fn, name)),
                    note=note,
                    title=f"{functions[fn].label}({name}): this test carried {note}",
                )
            )
            _add(weights, (2, fn), (3, key), samples, classes)
        if len(rows) > max_params:
            rest = rows[max_params:]
            key = f"{fn}\x00{MORE_KEY}"
            label = f"+{len(rest)} more parameters"
            param_nodes.append(FlowNode(key=key, label=label, title=f"{functions[fn].label}: {label}"))
            _add(weights, (2, fn), (3, key), sum(s for _, (s, _c) in rest), max(c for _, (_s, c) in rest))
    function_nodes = [functions[k] for k in functions if k in kept]
    if hidden:
        label = f"+{len(hidden)} more functions"
        function_nodes.append(
            FlowNode(
                key=MORE_KEY,
                label=label,
                title=f"{label} with fewer sampled calls: the table below lists every function",
            )
        )
    columns: list[tuple[str, list[FlowNode]]] = [
        ("test", [test]),
        ("via", list(vias.values())),
        ("functions it called", function_nodes),
        ("parameters · classes this test carried", param_nodes),
    ]
    labels = {(0, test.key): test.label}
    labels.update({(1, k): v.label for k, v in vias.items()})
    labels.update({(2, n.key): n.label for n in function_nodes})
    labels.update({(3, n.key): n.label for n in param_nodes})
    edges = []
    for (a, b), (samples, classes) in weights.items():
        if b == (2, MORE_KEY):
            what = f"{_plural(samples, 'sampled call')} into {len(hidden)} functions not drawn"
        elif samples:
            what = f"{_plural(samples, 'sampled call')}, {_plural(classes, 'input class', 'input classes')}"
        else:
            what = "its body ran; the tracer sampled no call for this test"
        title = f"{labels[a]} → {labels[b]}: {what}"
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
    '<p class="rb-legend">Ribbon width = sampled calls; a hairline = one call (or none sampled for '
    "this test: its body ran). Shade: input classes this test carried "
    '<svg viewBox="0 0 64 10" class="sw-row" aria-hidden="true">'
    + "".join(
        f'<rect class="rb-l{i}" x="{(i - 1) * 16}" width="14" height="10" rx="2"/>' for i in range(1, 5)
    )
    + "</svg> 1, 2, 3–4, 5+. Every bar is a link.</p>"
)


def render_test_flow(test: FlowNode, calls: Sequence[TestCall], *, width: float = 1100.0) -> Markup:
    """One test's data-flow SVG: test → via → functions → parameters."""
    columns, edges = build_test_flow(test, calls)
    called = len({call.function.key for call in calls})
    legend = FLOW_LEGEND
    if called > MAX_FLOW_FUNCTIONS:
        legend = Markup(
            f'{FLOW_LEGEND}<p class="rb-legend" data-flow-capped>Drawn: the {MAX_FLOW_FUNCTIONS} functions '
            f"with the most sampled calls of the {called} this test called (the table lists every one), "
            f"and at most {MAX_FLOW_PARAMS} parameters per function.</p>"
        )
    return render_flow(
        columns,
        edges,
        width=width,
        label=f"Data flow of {test.label}: functions it called and the parameters it passed",
        legend=legend,
        empty="This test called no measured function in this run.",
    )
