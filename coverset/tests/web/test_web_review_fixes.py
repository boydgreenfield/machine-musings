"""Behaviour fixed after the UI review: honest counts, the return row, the gaps table, hidden chips,
error pages, the quarantine count, replay-safe pins, Gantt layout and the ready flag."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from quart.typing import TestClientProtocol as Client
from test_web_sse import frames
from web_helpers import HX, Demo
from werkzeug.datastructures import Headers

from coverset.analysis.functions import index_functions
from coverset.analysis.shapes import Catalogue
from coverset.facts.queries import TreeNode
from coverset.lang.base import ShapeObs
from coverset.web.facts_view import FunctionView
from coverset.web.render.shapegrid import GridCell, render_inlay
from coverset.web.render.timeline import (
    MIN_BLOCK_W,
    MIN_FAIL_W,
    Milestone,
    OrderRow,
    render_gantt,
    render_timeline,
    render_why_order,
)
from coverset.web.repos import gap_rows
from coverset.web.runstate import Block

# -- counts: band, cards and sticky comment agree ------------------------------------------------


def _band(html: str) -> str:
    return html.split('<div class="verdict-band"', 1)[1].split("</div>\n</div>", 1)[0]


async def test_band_counts_classes_of_changed_functions_like_the_cards(client: Client, demo: Demo) -> None:
    for number in (1, 2):
        html = await (await client.get(f"/repos/{demo.repo_id}/prs/{number}")).get_data(as_text=True)
        band = _band(html)
        found = re.search(
            r"<strong>(\d+)</strong> input class(?:es)? never exercised in changed functions", band
        )
        assert found, band
        reasons = re.findall(r'<div class="why">([^<]*)</div>', html)
        per_card = [int(m.group(1)) for r in reasons if (m := re.search(r"(\d+) input class", r))]
        assert int(found.group(1)) == sum(per_card)
        more = re.search(r'title="[^"]+">(\d+) more in invariant targets</a>', band)
        assert more and int(more.group(1)) > 0
        assert f'href="/repos/{demo.repo_id}/invariants?run=' in band
    outbox = await (await client.get("/forge/outbox?method=upsert_pr_comment")).get_data(as_text=True)
    assert (
        "input classes never exercised in changed functions</strong> · 18 more in invariant targets" in outbox
    )
    assert "cents_conserved: 6 tests" not in outbox and "linked tests: " in outbox


async def test_card_shows_an_invariant_linked_through_its_failing_test(client: Client, demo: Demo) -> None:
    html = await (await client.get(f"/repos/{demo.repo_id}/prs/1")).get_data(as_text=True)
    card = html.split(">refund<", 1)[1].split("</summary>", 1)[0]
    assert "cents_conserved violated" in card and "via test_refund_partial" in card


# -- shape grid: the return row shows outputs only ---------------------------------------------------


async def test_return_row_has_only_filled_cells(client: Client, demo: Demo) -> None:
    pr2 = await (await client.get(f"/repos/{demo.repo_id}/prs/2")).get_data(as_text=True)
    key = re.search(r"/prs/2/functions/([0-9a-f]{16})", pr2).group(1)  # type: ignore[union-attr]
    grid = await (
        await client.get(f"/repos/{demo.repo_id}/functions/{key}/shapes?run={demo.pr2_run}", headers=HX)
    ).get_data(as_text=True)
    returns = re.findall(r'<g class="sg-cell([^"]*)"[^>]*aria-label="(return:[^"]+)"', grid)
    assert returns, "validate_email returns something"
    assert all(cls == "" and "returned in" in label for cls, label in returns)
    header = re.search(r"Input classes (\d+) of (\d+)", grid)
    hollow = len(re.findall(r'class="sg-cell is-hollow"', grid))
    assert header and int(header.group(2)) - int(header.group(1)) == hollow
    url = re.search(r'hx-get="([^"]+cls=return%3A[^"]+|[^"]+cls=return:[^"]+)"', grid)
    assert url
    detail = await (await client.get(url.group(1).replace("&amp;", "&"), headers=HX)).get_data(as_text=True)
    assert "returned in" in detail and "Hypothesis" not in detail and "never exercised" not in detail


# -- inlay chips name what the specific-family filter hides ----------------------------------------


SOURCE = """\
def parse(text: str) -> int:
    return len(text)
"""


def _obs(key: str, samples: int) -> ShapeObs:
    return ShapeObs(
        ("m.py", "parse", 1),
        "text",
        key,
        "tests/t.py::test_a",
        samples,
        ("tests/t.py::test_a",),
        False,
        "'x'",
        "tests/t.py::test_a",
    )


def test_chips_return_hidden_cells_and_say_so() -> None:
    region = next(r for r in index_functions("m.py", SOURCE) if r.qualname == "parse")
    observations = [_obs("str|ascii|len:2-10", 4), _obs("str|ascii|kind:duration/single-unit|len:2-10", 3)]
    cov = Catalogue().function_input_cov(region, observations, None, annotations={"text": "str"})
    view = FunctionView(region, cov, tuple(observations), {}, {"text": "str"}, "int", (), 100.0, None)
    chips = view.chips()
    assert chips
    _param, _annotation, shown, hidden = chips[0]
    assert len(shown) + len(hidden) == len(view.cells("text"))
    html = str(render_inlay(chips, note="", shapes_url="/s"))
    if hidden:
        assert f"+{len(hidden)} more (" in html
        if any(not c.observed for c in hidden):
            assert "chip chip-more chip-hollow" in html
    missing = [c for c in [*shown, *hidden] if not c.observed]
    assert len(missing) == view.hollow


def test_inlay_more_chip_is_hollow_only_when_a_hidden_class_is_missing() -> None:
    seen = GridCell("t:ascii", "str", True, 3)
    gap = GridCell("t:unicode", "str", False, 0)
    shown = [GridCell("t:single-unit", "quantity", True, 2)]
    html = str(render_inlay([("t", "str", shown, [seen])], note="", shapes_url="/s"))
    assert '<span class="chip chip-more" ' in html and "+1 more (ascii)" in html
    html = str(render_inlay([("t", "str", shown, [gap, seen])], note="", shapes_url="/s"))
    assert "chip chip-more chip-hollow" in html and "+2 more (unicode, ascii)" in html


async def test_source_header_count_matches_the_chips(client: Client, demo: Demo) -> None:
    url = f"/repos/{demo.repo_id}/files/src/ledger/durations/parse.py?run={demo.pr2_run}"
    html = await (await client.get(url)).get_data(as_text=True)
    header = re.search(r"input classes (\d+) of (\d+) \(", html)
    assert header
    inlays = re.findall(r'<div class="inlay">(.*?)<details class="fn-grid"', html, re.S)
    hollow = sum(len(re.findall(r'class="chip chip-hollow"', i)) for i in inlays)
    hidden = 0
    for inlay in inlays:
        for title in re.findall(
            r'class="chip chip-more chip-hollow" title="[^"]*\((\d+) never exercised', inlay
        ):
            hidden += int(title)
    assert hollow + hidden == int(header.group(2)) - int(header.group(1))
    # functions that expect no input classes get no inlay at all
    roles = await (
        await client.get(f"/repos/{demo.repo_id}/files/src/ledger/billing/invoices.py?run={demo.pr2_run}")
    ).get_data(as_text=True)
    assert "no input classes expected" not in roles


# -- map: gaps, never called, left out --------------------------------------------------------------


def test_gap_rows_separate_never_called_and_quiet_leaves() -> None:
    leaves = [
        TreeNode("a.py::<module>", "function", 5, 5, None),
        TreeNode("a.py::prop", "function", 1, 1, None, func_key="p", def_line=3),
        TreeNode("a.py::unused", "function", 4, 0, None, func_key="u", def_line=9),
        TreeNode("a.py::f", "function", 10, 10, 0.4, func_key="f", def_line=20),
        TreeNode("a.py::g", "function", 10, 5, 0.5, func_key="g", def_line=40),
    ]
    gaps, never, quiet = gap_rows(leaves)
    assert [g["name"] for g in gaps] == ["f", "g"] and gaps[0]["gap"] == 60 and gaps[1]["gap"] == 0
    assert [g["name"] for g in never] == ["unused"]
    assert quiet == {"functions": 1, "modules": 1}


async def test_map_table_never_calls_executed_code_never_called(client: Client, demo: Demo) -> None:
    html = await (await client.get(f"/repos/{demo.repo_id}/map")).get_data(as_text=True)
    table = html.split("<h2>Largest gaps</h2>", 1)[1]
    gaps, _, rest = table.partition("Never called by a test")
    assert "never called" not in gaps.lower() and "n/a" not in gaps
    assert "can_manage_billing" in rest and "100%" not in rest.split("</table>", 1)[0]
    assert "Left out:" in rest and "module-level code" in rest
    assert 'class="tm-leaf tm-module"' in html


# -- error pages ------------------------------------------------------------------------------------


async def test_not_found_is_a_styled_page_with_a_specific_message(client: Client, demo: Demo) -> None:
    response = await client.get("/repos/999")
    assert response.status_code == 404
    html = await response.get_data(as_text=True)
    assert 'class="top"' in html and "Not found" in html and "no repository with id 999" in html
    fragment = await client.get(f"/repos/{demo.repo_id}/functions/0000000000000000/shapes", headers=HX)
    assert fragment.status_code == 404
    body = await fragment.get_data(as_text=True)
    assert 'class="top"' not in body and "error-page" in body
    unknown = await client.get("/no/such/page")
    assert unknown.status_code == 404 and "Nothing lives at /no/such/page." in await unknown.get_data(
        as_text=True
    )


async def test_machine_paths_keep_their_error_bodies(client: Client) -> None:
    response = await client.get("/v1/nope")
    assert response.status_code == 404 and response.mimetype == "application/json"
    assert (await response.get_json()) == {"error": "not_found"}


async def test_server_error_page(app, client: Client) -> None:  # type: ignore[no-untyped-def]
    from werkzeug.exceptions import InternalServerError

    from coverset.web import _server_error

    async with app.test_request_context("/repos/1"):
        response = await _server_error(InternalServerError())
        assert response.status_code == 500  # type: ignore[union-attr]
        body = await response.get_data(as_text=True)  # type: ignore[union-attr]
        assert "Something went wrong" in body and "Traceback" not in body


# -- tests page: the quarantine toggle updates the page-head count ----------------------------------


async def test_quarantine_toggle_returns_the_count_out_of_band(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/repos/{demo.repo_id}/tests")).get_data(as_text=True)
    assert '<span id="quarantine-count">0 quarantined</span>' in page
    token = re.search(r'"X-CSRF-Token": "([^"]+)"', page).group(1)  # type: ignore[union-attr]
    test_id = int(re.search(r'id="test-(\d+)"', page).group(1))  # type: ignore[union-attr]
    url = f"/repos/{demo.repo_id}/tests/{test_id}/quarantine"
    headers = Headers({**HX, "X-CSRF-Token": token})
    try:
        body = await (await client.post(url, headers=headers, form={"quarantined": "1"})).get_data(
            as_text=True
        )
        assert '<span id="quarantine-count" hx-swap-oob="true">1 quarantined</span>' in body
    finally:
        body = await (await client.post(url, headers=headers, form={"quarantined": "0"})).get_data(
            as_text=True
        )
    assert '<span id="quarantine-count" hx-swap-oob="true">0 quarantined</span>' in body


# -- run page ---------------------------------------------------------------------------------------


async def test_why_order_lists_risk_signals_and_sums_up_the_rest(client: Client, demo: Demo) -> None:
    html = await (await client.get(f"/runs/{demo.pr1_run}")).get_data(as_text=True)
    panel = html.split('id="run-order"', 1)[1].split("</table>", 1)[0]
    scores = [float(s) for s in re.findall(r'<td class="num">(\d+\.\d)</td>', panel)]
    assert scores and all(s > 0 for s in scores)
    assert "tied at 8.0 with 2 others" in panel and "ran 1st; file order would have run it 66th" in panel
    more = re.search(r"(\d+) more tests with no risk signal, then ", panel)
    assert more and int(more.group(1)) + len(scores) == 100
    assert "p50 0.00 s" not in html and "cost $" not in html  # local executor: no cost line
    assert "scored against" not in html or "(merge-base)" in html


async def test_replay_pins_a_failure_only_after_replaying_it(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/runs/{demo.pr1_run}?replay=1&speed=0.25")).get_data(as_text=True)
    order = page.split('id="run-order"', 1)[1].split("</table>", 1)[0]
    assert 'class="pinned' not in order and "would have run it" not in order and "is-fail" not in order
    events = frames(
        await (await client.get(f"/runs/{demo.pr1_run}/events?replay=1&speed=100")).get_data(as_text=True)
    )
    orders = [d for _, e, d in events if e == "order"]
    pinned = [("pinned" in d) for d in orders]
    assert pinned[0] is False and pinned[-1] is True
    first = pinned.index(True)
    failures = [d for _, e, d in events if e == "failures"]
    assert "test_refund_partial" in failures[first]
    assert all(" open" not in re.sub(r"data-keep-open", "", d) for d in failures)
    assert all("data-keep-open" in d for d in failures if "<details" in d)
    full = await (await client.get(f"/runs/{demo.pr1_run}")).get_data(as_text=True)
    assert re.search(r'<details class="section failure" id="failure-[0-9a-f]{12}" data-keep-open open>', full)


async def test_frozen_replay_after_the_verdict_shows_fail(client: Client, demo: Demo) -> None:
    ((timeline,),) = demo.rows("SELECT timeline FROM runs WHERE id = :id", {"id": demo.pr1_run})
    marks = json.loads(timeline)
    origin = datetime.fromisoformat(marks.get("webhook_received") or marks["queued"])
    freeze = (datetime.fromisoformat(marks["verdict"]) - origin).total_seconds() + 0.1
    events = frames(
        await (
            await client.get(f"/runs/{demo.pr1_run}/events?replay=1&speed=100&freeze={freeze:.2f}")
        ).get_data(as_text=True)
    )
    head = [d for _, e, d in events if e == "head"][-1]
    gantt = [d for _, e, d in events if e == "gantt"][-1]
    assert ">FAIL<" in head and "gt-failure" in gantt and "first failure" in gantt


def test_timeline_marks_a_failure_reported_before_confirmation() -> None:
    marks = [Milestone("queued", "queued", 0.0), Milestone("failure_reported", "failure reported", 0.8)]
    html = str(render_timeline(marks, span=2.0))
    assert 'class="tl-tick is-failure is-provisional"' in html and "failure reported" in html


def test_timeline_breaks_a_ghost_bar_far_longer_than_the_run() -> None:
    html = str(
        render_timeline(
            [Milestone("queued", "queued", 0.0), Milestone("finished", "finished", 2.0)], ghost=180.0
        )
    )
    assert "tl-break" in html and "not to scale" in html
    near = str(
        render_timeline(
            [Milestone("queued", "queued", 0.0), Milestone("finished", "finished", 2.0)], ghost=3.0
        )
    )
    assert "tl-break" not in near


def _widths(html: str, cls: str) -> list[float]:
    return [float(w) for w in re.findall(rf'gt-block {cls}" x="[\d.]+" y="[\d.]+" width="([\d.]+)"', html)]


def _xs(html: str) -> list[float]:
    return [float(x) for x in re.findall(r'gt-block gt-(?:pass|fail)" x="([\d.]+)"', html)]


def test_gantt_dense_lane_falls_back_to_run_order() -> None:
    blocks = [Block(f"t{i}", 0.80 + i * 0.001, 0.801 + i * 0.001, "passed") for i in range(60)]
    blocks[10] = Block("bad", blocks[10].start, blocks[10].end, "failed")
    html = str(render_gantt({0: blocks}, span=2.0, lane_bounds={0: (0.7, 1.0)}, failure_at=1.2))
    assert "in run order" in html
    assert min(_widths(html, "gt-pass")) >= MIN_BLOCK_W and min(_widths(html, "gt-fail")) >= MIN_FAIL_W
    passes = [
        (float(x), float(w))
        for x, w in re.findall(r'gt-block gt-pass" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', html)
    ]
    assert all(a[0] + a[1] <= b[0] + 1e-6 for a, b in zip(passes, passes[1:], strict=False)), "no overlap"
    svg = html.split("</svg>", 1)[0]
    assert svg.rfind("gt-fail") > svg.rfind("gt-pass"), "failures are drawn last"
    assert 'class="gt-failure"' in html and "first failure 1.2 s" in html


def test_gantt_sparse_lane_keeps_the_time_axis() -> None:
    blocks = [Block("a", 0.0, 0.5, "passed"), Block("b", 0.5, 1.0, "passed")]
    html = str(render_gantt({0: blocks}, span=1.0, lane_bounds={0: (0.0, 1.0)}))
    assert "in run order" not in html and "gt-failure" not in html


def test_why_order_rest_row_and_tie_note() -> None:
    rows = [
        OrderRow(
            1,
            "t::a",
            8.0,
            {"new_or_changed": 8.0},
            "in changed file t",
            "failed",
            66,
            True,
            True,
            tie="tied at 8.0 with 2 others; wave 1 runs these tests by name",
        )
    ]
    html = str(render_why_order(rows, rest=96, rest_note="then shortest p50 first"))
    assert (
        "tied at 8.0 with 2 others" in html
        and "96 more tests with no risk signal, then shortest p50 first" in html
    )


# -- static contracts ---------------------------------------------------------------------------------


def test_ready_flag_settles_only_on_after_request() -> None:
    import coverset.web

    script = (Path(coverset.web.__file__).parent / "static" / "app.js").read_text()
    decrements = re.findall(
        r'addEventListener\("(htmx:[a-zA-Z]+)", function \(\) \{\s*pending = Math\.max', script
    )
    assert decrements == ["htmx:afterRequest"]
    assert "Could not load this section (HTTP " in script and "htmx:responseError" in script
    assert "beforeAttributeUpdated" in script and "data-keep-open" in script


def test_score_component_colours_avoid_the_coverage_and_status_hues() -> None:
    import coverset.web

    css = (Path(coverset.web.__file__).parent / "static" / "app.css").read_text().lower()
    components = re.findall(r"--c\d: light-dark\((#[0-9a-f]{6}), (#[0-9a-f]{6})\)", css)
    assert len(components) == 7
    reserved = {"#0072b2", "#56b4e9", "#d55e00", "#f0782a", "#1a7f37", "#46c165", "#c62828", "#ff6b60"}
    assert not reserved & {c for pair in components for c in pair}
