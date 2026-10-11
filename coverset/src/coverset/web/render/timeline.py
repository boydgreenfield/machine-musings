"""Run-page charts: the timeline strip, the shard Gantt chart and the "why this order" bars.

* Timeline strip: the run's milestones (webhook received … finished) on one time axis starting at
  the webhook (else when the run was queued), with an optional grey ghost bar for the measured cold
  path from ``docs/numbers.json``. A failed test reported before its retry confirmed it shows as a
  dashed, provisional "failure reported" tick. Milestones also appear as a list, so nothing is
  colour-only.
* Gantt: one lane per shard; blue = passed, vermilion with a heavier edge = failed, dotted yellow =
  flaky (failed, then passed on retry), grey = skipped/xfail, hollow dashed = tests still queued.
  Blocks are at least :data:`MIN_BLOCK_W` wide (failures :data:`MIN_FAIL_W`, drawn last); a lane
  whose blocks would overlap at that width is laid out in run order instead (each block pushed
  right of the previous one), and a full-height vermilion rule marks the first failure.
* Why this order: the tests with a risk signal (score above 0, or in wave 1) as stacked bars of
  their score components (fixed legend colours), each with its one-line explanation; failing tests
  are pinned on top with how a tie was broken, and one muted row sums up the rest.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from markupsafe import Markup

from coverset.web.render.html import attr, breakable, esc, fmt_secs, num, safe_href, truncate
from coverset.web.runstate import Block

__all__ = [
    "COMPONENTS",
    "MILESTONES",
    "MIN_BLOCK_W",
    "MIN_FAIL_W",
    "Milestone",
    "OrderRow",
    "lane_layout",
    "render_gantt",
    "render_timeline",
    "render_why_order",
]

MIN_BLOCK_W = 6.0
"""Narrowest Gantt block (px); a test never shrinks into a sliver."""
MIN_FAIL_W = 10.0
"""Narrowest failed or flaky block (px)."""
BLOCK_GAP = 1.0
GHOST_BREAK = 4.0
"""A cold path longer than this many run axes is drawn broken instead of to scale."""

MILESTONES: tuple[tuple[str, str], ...] = (
    ("webhook_received", "webhook received"),
    ("queued", "queued"),
    ("sandbox_ready", "sandbox ready"),
    ("first_test", "first test"),
    ("wave1_finished", "wave 1 done"),
    ("first_failure", "first failure"),
    ("verdict", "verdict"),
    ("finished", "finished"),
)

COMPONENTS: tuple[tuple[str, str], ...] = (
    ("failed_last_run", "failed last run"),
    ("new_or_changed", "new or changed test"),
    ("impacted_by_diff", "covers the change"),
    ("cov_overlap_frac", "share of the change"),
    ("fail_decay", "failed recently"),
    ("recently_flaky", "recently flaky"),
    ("cofail_norm", "fails with others"),
)
"""Score components in legend order (fixed colours ``c1``..``c7``)."""


@dataclass(frozen=True, slots=True)
class Milestone:
    """One timeline key with its offset in seconds from the clock start."""

    key: str
    label: str
    at: float


def milestones(
    timeline: Mapping[str, str], *, until: datetime | None = None
) -> tuple[list[Milestone], datetime | None]:
    """Milestones in order with offsets, and the clock origin (webhook, else queued)."""
    parsed: dict[str, datetime] = {}
    for key, _label in MILESTONES:
        value = timeline.get(key)
        if not value:
            continue
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            continue
        if until is None or moment <= until:
            parsed[key] = moment
    origin = parsed.get("webhook_received") or parsed.get("queued")
    if origin is None and parsed:
        origin = min(parsed.values())
    if origin is None:
        return [], None
    out = [
        Milestone(key, label, (parsed[key] - origin).total_seconds())
        for key, label in MILESTONES
        if key in parsed
    ]
    return out, origin


def _fmt_s(seconds: float) -> str:
    return fmt_secs(seconds)


def render_timeline(
    marks: Sequence[Milestone],
    *,
    span: float | None = None,
    ghost: float | None = None,
    ghost_label: str = "measured cold path",
    ghost_mark: float | None = None,
    ghost_mark_label: str = "first failure",
    width: float = 1100.0,
) -> Markup:
    """The strip SVG plus an ordered list of the same milestones.

    ``ghost`` draws a grey bar of that many seconds under the strip (``ghost_label`` names it) and
    ``ghost_mark`` a tick on it (``ghost_mark_label``, e.g. when the cold path saw its first
    failure).
    """
    if not marks:
        return Markup('<p class="empty">No timeline recorded yet.</p>')
    end = max(m.at for m in marks)
    run_axis = max(span or 0.0, end, 0.001)
    # A cold path many times longer than the run would squash every milestone into the first few
    # pixels: then the axis fits the run and the ghost bar is drawn broken (not to scale).
    broken = bool(ghost) and ghost > GHOST_BREAK * run_axis  # type: ignore[operator]
    axis = run_axis if broken else max(run_axis, ghost or 0.0)
    left, right = 8.0, width - 16.0
    scale = (right - left) / axis
    bar_y = 44.0
    near_above, far_above, near_below, far_below = 30.0, 16.0, 72.0, 86.0
    order = (near_above, near_below, far_above, far_below)
    placed: list[tuple[float, float, float, float]] = []  # (row y, left, right, tick x)
    parts: list[str] = [
        f'<rect class="tl-track" x="{num(left)}" y="{num(bar_y)}" width="{num(right - left)}" height="8" rx="4"/>',
        f'<rect class="tl-run" x="{num(left)}" y="{num(bar_y)}" width="{num(max(2.0, end * scale))}" height="8" rx="4"/>',
    ]

    def crosses(row: float, a: float, b: float, tick: float) -> int:
        """How many conflicts placing a label at ``row`` spanning ``a..b`` with ``tick`` causes."""
        nearer = {near_above: (), far_above: (near_above,), near_below: (), far_below: (near_below,)}[row]
        farther = {near_above: (far_above,), far_above: (), near_below: (far_below,), far_below: ()}[row]
        bad = 0
        for y, lo, hi, t in placed:
            if y == row and not (b + 8 < lo or a > hi + 8):
                bad += 2
            if y in farther and a - 3 <= t <= b + 3:
                bad += 1
            if y in nearer and lo - 3 <= tick <= hi + 3:
                bad += 1
        return bad

    for mark in marks:
        x = left + mark.at * scale
        text_w = (len(mark.label) + len(_fmt_s(mark.at)) + 1) * 6.3
        lx = x + 4 if x + 4 + text_w <= right else x - 4 - text_w
        row = min(order, key=lambda y: (crosses(y, lx, lx + text_w, x), order.index(y)))
        placed.append((row, lx, lx + text_w, x))
        cls = {
            "first_failure": "tl-tick is-failure",
            "failure_reported": "tl-tick is-failure is-provisional",
        }.get(mark.key, "tl-tick")
        tick_top, tick_bottom = (row + 3, bar_y + 8) if row < bar_y else (bar_y, row - 11)
        parts.append(
            f'<line class="{cls}" x1="{num(x)}" x2="{num(x)}" y1="{num(tick_top)}" y2="{num(tick_bottom)}"/>'
        )
        parts.append(
            f'<text class="tl-label" x="{num(lx)}" y="{num(row)}">'
            f'{esc(mark.label)} <tspan class="tl-num">{esc(_fmt_s(mark.at))}</tspan></text>'
        )
    height = 94.0
    if ghost:
        gy = 104.0
        ghost_w = (right - left) if broken else ghost * scale
        parts.append(
            f'<rect class="tl-ghost" x="{num(left)}" y="{num(gy)}" width="{num(ghost_w)}" height="8" rx="4"/>'
        )
        note = ""
        if broken:
            bx = left + (right - left) * 0.62
            parts.append(
                f'<path class="tl-break" d="M{num(bx - 3)},{num(gy + 11)} L{num(bx + 1)},{num(gy - 3)}'
                f' M{num(bx + 3)},{num(gy + 11)} L{num(bx + 7)},{num(gy - 3)}"/>'
            )
            note = f" (bar not to scale: {ghost / run_axis:.0f}× this axis)"
        mark_text = ""
        if ghost_mark is not None and 0 < ghost_mark <= ghost:
            mx = left + ghost_w * ghost_mark / ghost
            parts.append(
                f'<line class="tl-tick is-failure" x1="{num(mx)}" x2="{num(mx)}"'
                f' y1="{num(gy - 3)}" y2="{num(gy + 11)}"/>'
            )
            mark_text = f" · {ghost_mark_label} {_fmt_s(ghost_mark)}"
        parts.append(
            f'<text class="tl-label tl-ghost-label" x="{num(left)}" y="{num(gy + 22)}">'
            f'{esc(ghost_label)} <tspan class="tl-num">{esc(_fmt_s(ghost))}</tspan>{esc(mark_text)}{esc(note)}</text>'
        )
        height = 132.0
    items = "".join(
        f'<li data-key="{attr(m.key)}"><span>{esc(m.label)}</span> <span class="num">{esc(_fmt_s(m.at))}</span></li>'
        for m in marks
    )
    return Markup(
        f'<svg class="timeline-strip" viewBox="0 0 {num(width)} {num(height)}" role="img"'
        f' aria-label="Run timeline">{"".join(parts)}</svg>'
        f'<ol class="milestones">{items}</ol>'
    )


def lane_layout(
    blocks: Sequence[Block], *, x_of: Callable[[float], float], scale: float, right: float
) -> tuple[list[tuple[Block, float, float]], bool]:
    """``(block, x, width)`` per block, and whether the lane fell back to run order.

    On the time axis a block spans its start to its end, at least :data:`MIN_BLOCK_W` wide
    (:data:`MIN_FAIL_W` for failures). When those widths would make blocks overlap, the lane is
    laid out in run order: each block starts no earlier than its time and right after the previous
    block, and if that runs past ``right`` every block shrinks evenly (failures stay widest).
    """
    if not blocks:
        return [], False
    widths = [
        max(
            MIN_FAIL_W if _block_class(b) in ("gt-fail", "gt-flaky") else MIN_BLOCK_W,
            (b.end - b.start) * scale - 0.75,
        )
        for b in blocks
    ]
    xs = [x_of(b.start) for b in blocks]
    overlap = any(xs[i] + widths[i] > xs[i + 1] + 0.01 for i in range(len(blocks) - 1))
    if not overlap:
        return list(zip(blocks, xs, widths, strict=True)), False
    placed: list[float] = []
    cursor = xs[0]
    for x, w in zip(xs, widths, strict=True):
        x = max(x, cursor)
        placed.append(x)
        cursor = x + w + BLOCK_GAP
    if cursor > right and len(blocks) > 1:
        start = placed[0]
        fails = [i for i, b in enumerate(blocks) if _block_class(b) in ("gt-fail", "gt-flaky")]
        room = right - start - BLOCK_GAP * len(blocks) - MIN_FAIL_W * len(fails)
        others = len(blocks) - len(fails)
        each = max(1.5, room / others) if others else 0.0
        widths = [MIN_FAIL_W if i in fails else each for i in range(len(blocks))]
        placed, cursor = [], start
        for w in widths:
            placed.append(cursor)
            cursor += w + BLOCK_GAP
    return list(zip(blocks, placed, widths, strict=True)), True


def render_gantt(
    lanes: Mapping[int, Sequence[Block]],
    *,
    span: float,
    queued: Mapping[int, int] | None = None,
    lane_status: Mapping[int, str] | None = None,
    lane_bounds: Mapping[int, tuple[float, float | None]] | None = None,
    failure_at: float | None = None,
    failure_label: str = "first failure",
    width: float = 1100.0,
) -> Markup:
    """Shard lanes (seconds on x from the clock origin, ``span`` wide) and, with ``failure_at``, a
    full-height rule labelled ``failure_label`` and the time."""
    if not lanes:
        return Markup('<p class="empty">No shard has reported a test yet.</p>')
    queued = queued or {}
    lane_status = lane_status or {}
    lane_bounds = lane_bounds or {}
    label_w, right_pad, lane_h, gap = 74.0, 12.0, 22.0, 8.0
    top = 16.0 if failure_at is not None and 0 <= failure_at <= span else 0.0
    axis_h = top + 22.0
    span = max(span, 0.001)
    plot_w = width - label_w - right_pad
    scale = plot_w / span
    shards = sorted(lanes)
    height = axis_h + len(shards) * (lane_h + gap)

    def x_of(seconds: float) -> float:
        return label_w + seconds * scale

    parts: list[str] = []
    for tick in _ticks(span):
        x = x_of(tick)
        parts.append(
            f'<line class="gt-grid" x1="{num(x)}" x2="{num(x)}" y1="{num(axis_h - 4)}" y2="{num(height)}"/>'
        )
        parts.append(
            f'<text class="gt-axis" x="{num(x)}" y="{num(top + 12)}" text-anchor="middle">{esc(_fmt_s(tick))}</text>'
        )
    ordered_lanes = 0
    for row, shard in enumerate(shards):
        y = axis_h + row * (lane_h + gap)
        status = lane_status.get(shard, "")
        placed, by_order = lane_layout(lanes[shard], x_of=x_of, scale=scale, right=width - right_pad)
        ordered_lanes += by_order
        start, finish = lane_bounds.get(shard, (None, None))  # type: ignore[assignment]
        lane_note = " · in run order" if by_order else ""
        parts.append(
            f'<text class="gt-lane-label" x="0" y="{num(y + 15)}">shard {shard}'
            f'<tspan class="gt-lane-status"> {esc(status)}{esc(lane_note)}</tspan></text>'
        )
        if start is not None:
            stop = finish if finish is not None else (lanes[shard][-1].end if lanes[shard] else start)
            x_start, x_stop = x_of(start), x_of(stop)
            if placed:
                x_stop = max(x_stop, placed[-1][1] + placed[-1][2])
            parts.append(
                f'<rect class="gt-lane" x="{num(x_start)}" y="{num(y)}" width="{num(max(1.0, x_stop - x_start))}"'
                f' height="{num(lane_h)}" rx="3"><title>shard {shard}{esc(lane_note)}</title></rect>'
            )
        marked: list[str] = []
        last_x = None
        for block, x, w in placed:
            cls = _block_class(block)
            title = f"{block.nodeid}: {block.outcome}{' (flaky: passed on retry)' if block.flaky else ''}"
            title += f" · reported at {_fmt_s(block.end)}"
            (marked if cls in ("gt-fail", "gt-flaky") else parts).append(
                f'<rect class="gt-block {cls}" x="{num(x)}" y="{num(y + 2)}" width="{num(w)}"'
                f' height="{num(lane_h - 4)}" rx="1.5"><title>{esc(title)}</title></rect>'
            )
            last_x = x + w
        parts.extend(marked)
        waiting = queued.get(shard, 0)
        if waiting > 0:
            x = (last_x if last_x is not None else x_of(start or 0.0)) + 2
            w = max(24.0, min(width - right_pad - x, 120.0))
            if w > 4:
                parts.append(
                    f'<rect class="gt-block gt-queued" x="{num(x)}" y="{num(y + 2)}" width="{num(w)}"'
                    f' height="{num(lane_h - 4)}" rx="1.5"><title>{waiting} tests queued</title></rect>'
                    f'<text class="gt-queued-label" x="{num(x + 5)}" y="{num(y + 15)}">{waiting} queued</text>'
                )
    if top:
        assert failure_at is not None
        x = x_of(failure_at)
        text = f"{failure_label} {_fmt_s(failure_at)}"
        anchor = "end" if x + len(text) * 6.2 > width - 2 else "start"
        tx = x - 4 if anchor == "end" else x + 4
        parts.append(
            f'<line class="gt-failure" x1="{num(x)}" x2="{num(x)}" y1="2" y2="{num(height)}"/>'
            f'<text class="gt-failure-label" x="{num(tx)}" y="11" text-anchor="{anchor}">'
            f"{esc(text)}</text>"
        )
    order_note = (
        '<li class="gt-note">lanes marked “in run order” are too dense for the time axis: their tests'
        " follow one another from the lane's start</li>"
        if ordered_lanes
        else ""
    )
    legend = (
        '<ul class="legend gt-legend">'
        '<li><svg class="sw" viewBox="0 0 14 14" aria-hidden="true"><rect class="gt-block gt-pass" width="14" height="14" rx="2"/></svg>passed</li>'
        '<li><svg class="sw" viewBox="0 0 14 14" aria-hidden="true"><rect class="gt-block gt-fail" x="1" y="1" width="12" height="12" rx="2"/></svg>failed</li>'
        '<li><svg class="sw" viewBox="0 0 14 14" aria-hidden="true"><rect class="gt-block gt-flaky" x="1" y="1" width="12" height="12" rx="2"/></svg>flaky (passed on retry)</li>'
        '<li><svg class="sw" viewBox="0 0 14 14" aria-hidden="true"><rect class="gt-block gt-other" width="14" height="14" rx="2"/></svg>skipped or xfail</li>'
        '<li><svg class="sw" viewBox="0 0 14 14" aria-hidden="true"><rect class="gt-block gt-queued" x="1" y="1" width="12" height="12" rx="2"/></svg>queued</li>'
        f"{order_note}</ul>"
    )
    return Markup(
        f'<svg class="gantt" viewBox="0 0 {num(width)} {num(height)}" role="img"'
        f' aria-label="Tests per shard over time">{"".join(parts)}</svg>{legend}'
    )


def _block_class(block: Block) -> str:
    if block.flaky:
        return "gt-flaky"
    if block.outcome in ("failed", "error"):
        return "gt-fail"
    if block.outcome == "passed":
        return "gt-pass"
    return "gt-other"


def _ticks(span: float) -> list[float]:
    for step in (0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600):
        if span / step <= 8:
            break
    count = int(span / step) + 1
    return [round(i * step, 3) for i in range(count)]


@dataclass(frozen=True, slots=True)
class OrderRow:
    """One test in the priority order."""

    rank: int
    nodeid: str
    score: float
    components: Mapping[str, float]
    text: str
    outcome: str
    file_rank: int | None = None
    wave1: bool = False
    pinned: bool = False
    tie: str = ""
    """How a tie on the score was broken (pinned rows show it)."""
    listed: bool = True
    """Whether the row is listed for its own sake (a risk signal), not only because it is pinned."""


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def render_why_order(
    rows: Sequence[OrderRow],
    *,
    test_url: Callable[[str], str] | None = None,
    width: float = 260.0,
    rest: int = 0,
    rest_note: str = "",
) -> Markup:
    """A table: rank, test, stacked score bar and explanation; pinned rows first, then one muted
    row for the ``rest`` tests not listed (``rest_note`` says in what order they ran)."""
    if not rows and not rest:
        return Markup('<p class="empty">No priority order was recorded for this run.</p>')
    top = max((r.score for r in rows), default=1.0) or 1.0
    body: list[str] = []
    for row in sorted(rows, key=lambda r: (not r.pinned, r.rank)):
        x = 0.0
        segs: list[str] = []
        for index, (key, label) in enumerate(COMPONENTS, start=1):
            value = float(row.components.get(key, 0.0) or 0.0)
            if value <= 0:
                continue
            w = width * value / top
            segs.append(
                f'<rect class="c{index}" x="{num(x)}" y="2" width="{num(max(1.0, w - 2))}" height="12" rx="2">'
                f"<title>{esc(label)}: {value:.2f}</title></rect>"
            )
            x += w
        bar = (
            f'<svg class="score-bar" viewBox="0 0 {num(width)} 16" width="{num(width)}" height="16"'
            f' role="img" aria-label="{attr(f"score {row.score:.2f}")}">{"".join(segs)}</svg>'
        )
        name = breakable(truncate(row.nodeid, 90))
        if test_url is not None:
            name = Markup(f'<a href="{attr(safe_href(test_url(row.nodeid)))}">{name}</a>')
        note = esc(row.text)
        if row.pinned and row.file_rank:
            tie = f" · {esc(row.tie)}" if row.tie else ""
            note = Markup(
                f"<strong>ran {_ordinal(row.rank)}; file order would have run it {_ordinal(row.file_rank)}</strong>"
                f"{tie} · {esc(row.text)}"
            )
        outcome_cls = "is-fail" if row.outcome in ("failed", "error") else ""
        wave = '<span class="badge">wave 1</span>' if row.wave1 else ""
        body.append(
            f'<tr class="{"pinned " if row.pinned else ""}{outcome_cls}"><td class="num">{row.rank}</td>'
            f'<td class="test-cell"><span class="nodeid">{name}</span> {wave}<div class="why">{note}</div></td>'
            f'<td class="num">{row.score:.1f}</td><td>{bar}</td></tr>'
        )
    if rest:
        noun = "test" if rest == 1 else "tests"
        body.append(
            f'<tr class="why-rest"><td></td><td colspan="3">{rest} more {noun} with no risk signal'
            f"{(', ' + esc(rest_note)) if rest_note else ''}</td></tr>"
        )
    legend = "".join(
        f'<li><svg class="sw" viewBox="0 0 14 14" aria-hidden="true"><rect class="c{i}" width="14" height="14" rx="2"/></svg>{esc(label)}</li>'
        for i, (_key, label) in enumerate(COMPONENTS, start=1)
    )
    return Markup(
        '<table class="why-order"><thead><tr><th class="num">#</th><th>Test and reason</th>'
        '<th class="num">Score</th><th>Score components</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table><ul class="legend why-legend">{legend}</ul>'
    )
