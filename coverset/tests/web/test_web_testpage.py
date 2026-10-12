"""The test page (``/repos/<rid>/tests/<test_id>``) and the links to it, on the real demo data."""

from __future__ import annotations

import html as html_lib
import json
import re
import sqlite3
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import pytest
from markupsafe import Markup
from quart.typing import TestClientProtocol as Client
from web_helpers import HX, Demo, assert_csp_clean

from coverset.facts import queries
from coverset.facts.store import FactsStore
from coverset.lang.base import ShapeObs
from coverset.orchestrator.history import nodeid_hash
from coverset.web import testpage
from coverset.web.render.flow import FlowEdge, FlowNode, TestCall, render_flow, render_test_flow

SCRIPT = "<script>alert(1)</script>"
BREAKOUT = '"onmouseover="alert(1)'
JS_URL = "javascript:alert(1)"
EVIL = f"{SCRIPT}{BREAKOUT}{JS_URL}"
HOSTILE_NODEID = f"tests/test_evil.py::test_x[{SCRIPT}{BREAKOUT}'{JS_URL}]"

TAG_WITH_HANDLER = re.compile(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", re.IGNORECASE)
LIVE_JS_URL = re.compile(r"""(?:href|src|hx-get|hx-post)\s*=\s*["']?\s*javascript:""", re.IGNORECASE)


def assert_safe(text: str | Markup) -> None:
    body = str(text)
    assert not re.search(r"<script(?![^>]*\bsrc=\"/static/)", body, re.IGNORECASE), body[:400]
    assert not TAG_WITH_HANDLER.search(body), TAG_WITH_HANDLER.search(body)
    assert not LIVE_JS_URL.search(body), LIVE_JS_URL.search(body)


def _db(demo: Demo) -> sqlite3.Connection:
    return sqlite3.connect(demo.db_url.split("///", 1)[1])


async def _failing_test_url(client: Client, demo: Demo) -> str:
    """The test page of PR #1's failing test, followed from the run page's failures list."""
    run_page = await (await client.get(f"/runs/{demo.pr1_run}")).get_data(as_text=True)
    found = re.search(r'id="run-failures".*?<a class="nodeid test-link" href="([^"]+)"', run_page, re.S)
    assert found, "the failures list links its test"
    link = html_lib.unescape(found.group(1))
    assert link.startswith(f"/repos/{demo.repo_id}/tests/by-nodeid?") and f"run={demo.pr1_run}" in link
    response = await client.get(link)
    assert response.status_code == 302
    target = response.headers["Location"]
    assert re.fullmatch(rf"/repos/{demo.repo_id}/tests/\d+\?run={demo.pr1_run}", target)
    return target


async def test_test_page_tells_the_failing_tests_story(client: Client, demo: Demo) -> None:
    url = await _failing_test_url(client, demo)
    response = await client.get(url)
    assert response.status_code == 200
    page = await response.get_data(as_text=True)
    assert_csp_clean(page)
    assert 'id="test-identity"' in page and 'id="test-result"' in page
    assert 'class="oc oc-failed"' in page and 'class="longrepr"' in page
    assert "cents_conserved" in page  # the invariant the test is linked to
    assert 'class="history"' in page and 'aria-current="page"' in page  # the selected run's square
    assert 'class="flow-test"' in page and 'class="fl-link"' in page
    assert ">refund</code></a>" in page  # the function the test called, linked to its page
    assert re.search(r'<svg class="shape-strip"', page)
    assert "this test" in page and "any test" in page
    assert "data-test-lines" not in page  # the source view's ?test= lens replaced that payload
    fragment = await client.get(url, headers=HX)
    body = await fragment.get_data(as_text=True)
    assert fragment.status_code == 200 and "<!doctype" not in body.lower() and 'class="flow-test"' in body
    assert_csp_clean(body)


async def test_the_page_agrees_with_the_facts_queries(client: Client, demo: Demo) -> None:
    url = await _failing_test_url(client, demo)
    page = await (await client.get(url)).get_data(as_text=True)
    test_id = int(url.split("/tests/", 1)[1].split("?", 1)[0])
    con = _db(demo)
    try:
        nodeid = con.execute("SELECT nodeid FROM tests WHERE id = ?", (test_id,)).fetchone()[0]
    finally:
        con.close()
    store = FactsStore(demo.data_dir / "facts")
    touched = queries.test_touched(store, demo.repo_id, demo.pr1_run, nodeid)
    called = {fn.qualname for f in touched for fn in f.functions}
    assert "refund" in called
    for qualname in called:
        assert f">{html_lib.escape(qualname)}</code></a>" in page
    for f in touched:
        n = len(f.lines)
        assert (
            f'<code>{html_lib.escape(f.path)}</code> <span class="muted">· {n} line{"" if n == 1 else "s"} run'
            in page
        )
    shapes = queries.test_shapes(store, demo.repo_id, demo.pr1_run, nodeid)
    assert shapes and all(nodeid in o.tests or o.caller == nodeid for o in shapes)
    history = queries.test_history(store, demo.repo_id, nodeid, last_n=50)
    mine = next(r for r in history if r.run_id == demo.pr1_run)
    assert mine.outcome == "failed"


async def test_default_run_and_bad_ids(client: Client, demo: Demo) -> None:
    url = await _failing_test_url(client, demo)
    bare = url.split("?", 1)[0]
    default = await client.get(bare)
    assert default.status_code == 200  # the newest completed default-branch baseline
    html = await default.get_data(as_text=True)
    assert "In this run" in html
    assert (await client.get(f"/repos/{demo.repo_id}/tests/999999")).status_code == 404
    assert (await client.get(f"{bare}?run=not-a-run!")).status_code == 404
    assert (await client.get(f"{bare}?run=0123456789abcdef0123456789abcdef")).status_code == 404
    missing = await client.get(f"/repos/{demo.repo_id}/tests/by-nodeid?nodeid=tests/nope.py::test_nope")
    assert missing.status_code == 404
    assert (await client.get(f"/repos/{demo.repo_id}/tests/by-nodeid")).status_code == 404


async def test_every_listing_links_to_the_test_page(client: Client, demo: Demo) -> None:
    rid = demo.repo_id
    tests = await (await client.get(f"/repos/{rid}/tests")).get_data(as_text=True)
    assert re.search(rf'<a class="nodeid test-link" href="/repos/{rid}/tests/\d+">', tests)
    popover = await (
        await client.get(
            f"/repos/{rid}/files/src/ledger/durations/parse.py/lines/16/tests?run={demo.baseline_run}",
            headers=HX,
        )
    ).get_data(as_text=True)
    assert f'href="/repos/{rid}/tests/by-nodeid?' in popover
    pr2 = await (await client.get(f"/repos/{rid}/prs/2")).get_data(as_text=True)
    key = re.search(r"/prs/2/functions/([0-9a-f]{16})", pr2)
    assert key
    function = await (await client.get(f"/repos/{rid}/functions/{key.group(1)}?run={demo.pr2_run}")).get_data(
        as_text=True
    )
    assert "Top covering tests" in function and f'href="/repos/{rid}/tests/by-nodeid?' in function
    link = re.search(rf'href="(/repos/{rid}/tests/by-nodeid\?[^"]+)"', function)
    assert link
    query = parse_qs(urlparse(html_lib.unescape(link.group(1))).query)
    assert query["run"] == [demo.pr2_run] and query["nodeid"][0].startswith("tests/")
    followed = await client.get(html_lib.unescape(link.group(1)))
    assert followed.status_code == 302
    page = await client.get(followed.headers["Location"])
    assert page.status_code == 200


@pytest.fixture
def hostile_test(demo: Demo) -> Iterator[int]:
    """A test with a hostile nodeid, a failed result in the baseline run and a hostile failure text."""
    con = _db(demo)
    try:
        cur = con.execute(
            "INSERT INTO tests (repo_id, nodeid, nodeid_hash, family, quarantined) VALUES (?, ?, ?, ?, 0)",
            (demo.repo_id, HOSTILE_NODEID, nodeid_hash(HOSTILE_NODEID), HOSTILE_NODEID.split("[", 1)[0]),
        )
        test_id = int(cur.lastrowid or 0)
        con.execute(
            "INSERT INTO test_results (run_id, test_id, outcome, duration_ms, attempts, flaky, quarantined) "
            "VALUES (?, ?, 'failed', 12.0, 1, 0, 0)",
            (demo.baseline_run, test_id),
        )
        seq = con.execute(
            "SELECT coalesce(max(seq), 0) + 1000 FROM run_events WHERE run_id = ?", (demo.baseline_run,)
        ).fetchone()[0]
        con.execute(
            "INSERT INTO run_events (run_id, seq, kind, payload, at) VALUES (?, ?, 'test', ?, '2026-10-11 12:00:00')",
            (
                demo.baseline_run,
                seq,
                json.dumps({"nodeid": HOSTILE_NODEID, "outcome": "failed", "longrepr": EVIL}),
            ),
        )
        con.commit()
        yield test_id
    finally:
        con.execute(
            "DELETE FROM run_events WHERE run_id = ? AND payload LIKE ?", (demo.baseline_run, "%test_evil%")
        )
        con.execute(
            "DELETE FROM test_results WHERE test_id IN (SELECT id FROM tests WHERE nodeid = ?)",
            (HOSTILE_NODEID,),
        )
        con.execute("DELETE FROM tests WHERE nodeid = ?", (HOSTILE_NODEID,))
        con.commit()
        con.close()


async def test_hostile_nodeids_and_failure_text_are_escaped(
    client: Client, demo: Demo, hostile_test: int
) -> None:
    rid = demo.repo_id
    page = await (await client.get(f"/repos/{rid}/tests/{hostile_test}?run={demo.baseline_run}")).get_data(
        as_text=True
    )
    assert_safe(page)  # the text "javascript:" may appear escaped, never as a live URL
    assert "&lt;script&gt;" in page and 'class="oc oc-failed"' in page
    tests = await (await client.get(f"/repos/{rid}/tests")).get_data(as_text=True)
    assert_safe(tests)
    assert f'href="/repos/{rid}/tests/{hostile_test}"' in tests
    redirect = await client.get(f"/repos/{rid}/tests/by-nodeid", query_string={"nodeid": HOSTILE_NODEID})
    assert (
        redirect.status_code == 302 and redirect.headers["Location"] == f"/repos/{rid}/tests/{hostile_test}"
    )


async def test_hostile_reprs_on_the_page_are_escaped(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = await _failing_test_url(client, demo)
    real = queries.test_shapes

    def hostile(*args: object, **kwargs: object) -> list[ShapeObs]:
        rows = real(*args, **kwargs)  # type: ignore[arg-type]
        nodeid = args[3]
        return [
            ShapeObs(o.func, o.param, o.shape_key, o.caller, o.samples, o.tests, o.truncated, EVIL, nodeid)  # type: ignore[arg-type]
            for o in rows
        ]

    monkeypatch.setattr(testpage.queries, "test_shapes", hostile)
    page = await (await client.get(url)).get_data(as_text=True)
    assert_safe(page)
    assert "e.g. <code>&lt;script&gt;" in page


def test_flow_renderer_escapes_everything() -> None:
    test = FlowNode("t", EVIL, JS_URL, title=EVIL)
    function = FlowNode("f", EVIL, "/repos/1/functions/abc", note=EVIL)
    via = FlowNode("v", EVIL, "//evil.example/x")
    svg = render_test_flow(test, [TestCall(function, EVIL, JS_URL, 5, 3, via), TestCall(FlowNode("g", "g"))])
    assert_safe(svg)
    assert 'href="#"' in str(svg) and "/repos/1/functions/abc" in str(svg)
    assert 'class="flow-test"' in str(svg) and "rb-l3" in str(svg)
    generic = render_flow(
        [(EVIL, [FlowNode("a", EVIL)]), ("b", [FlowNode("b", "b", JS_URL)])],
        [FlowEdge((0, "a"), (1, "b"), 3, 9, EVIL)],
        label=EVIL,
        legend=Markup("<p>ok</p>"),
    )
    assert_safe(generic)
    assert "rb-l4" in str(generic)  # levels are clamped to 1-4
    assert str(render_flow([("a", [])], [], empty=EVIL)).startswith('<p class="empty">&lt;script&gt;')


def test_flow_layout_is_proportional_and_finite() -> None:
    test = FlowNode("t", "test_a")
    calls = [
        TestCall(FlowNode("f1", "allocate"), "parts", None, 30, 5),
        TestCall(FlowNode("f2", "refund"), "amount", None, 10, 1, FlowNode("f1", "allocate")),
        TestCall(FlowNode("f3", "noparams")),
    ]
    svg = str(render_test_flow(test, calls, width=800))
    assert "nan" not in svg.lower() and "inf" not in svg.lower()
    heights = [
        float(h) for h in re.findall(r'class="rb-node" x="[^"]+" y="[^"]+" width="4" height="([^"]+)"', svg)
    ]
    assert len(heights) == 1 + 1 + 3 + 2  # test, via, three functions, two parameters
    assert max(heights) == heights[0]  # the test carries every call


def test_test_decorations_reads_markers_and_given() -> None:
    source = (
        "import pytest\nfrom hypothesis import given, strategies as st\n\n"
        "pytestmark = pytest.mark.slow\n\n"
        "class TestBox:\n    pytestmark = [pytest.mark.db]\n\n"
        "    @pytest.mark.invariant('cents_conserved')\n    @given(st.integers())\n"
        "    def test_total(self, n):\n        pass\n\n"
        "@pytest.mark.parametrize('x', [1])\ndef test_plain(x):\n    pass\n"
    )
    found = testpage.test_decorations(source, "tests/t.py::TestBox::test_total")
    assert found is not None and found.hypothesis and found.invariant_ids == ("cents_conserved",)
    assert found.markers == ("slow", "db", "invariant") and found.line == 11
    plain = testpage.test_decorations(source, "tests/t.py::test_plain[1]")
    assert plain is not None and not plain.hypothesis and plain.markers == ("slow", "parametrize")
    assert testpage.test_decorations(source, "tests/t.py::test_missing") is None
    assert testpage.test_decorations("def (", "tests/t.py::test_plain") is None
    assert testpage.line_ranges([5, 1, 2, 3, 7, 8]) == [(1, 3), (5, 5), (7, 8)]


# -- the flow's counts and size --------------------------------------------------------------------


def test_flow_counts_a_call_once_however_many_parameters_it_passed() -> None:
    from coverset.web.render.flow import build_test_flow

    test = FlowNode("t", "test_refund_partial")
    refund = FlowNode("f", "refund")
    calls = [TestCall(refund, "total_cents", None, 1, 1), TestCall(refund, "weights", None, 1, 1)]
    _columns, edges = build_test_flow(test, calls)
    by_pair = {(e.source, e.target): e for e in edges}
    into = by_pair[((0, "t"), (2, "f"))]
    assert into.weight == 1 and "1 sampled call," in into.title  # one call, not one per parameter
    assert by_pair[((2, "f"), (3, "f\x00total_cents"))].weight == 1
    # through a caller: test -> via and via -> function count the call once too
    allocate = FlowNode("g", "allocate_cents")
    calls = [
        TestCall(allocate, "total_cents", None, 3, 1, refund),
        TestCall(allocate, "weights", None, 2, 1, refund),
    ]
    _columns, edges = build_test_flow(test, calls)
    weights = {(e.source, e.target): e.weight for e in edges}
    assert weights[((0, "t"), (1, "f"))] == 3 and weights[((1, "f"), (2, "g"))] == 3
    assert "Ribbon width = sampled calls; a hairline = one call" in str(render_test_flow(test, calls))


def test_flow_is_capped() -> None:
    from coverset.web.render.flow import MAX_FLOW_FUNCTIONS, MAX_FLOW_PARAMS

    test = FlowNode("t", "test_big")
    calls = [
        TestCall(FlowNode(f"k{i}", f"fn{i}", f"/f/{i}"), p, None, 1 + i % 7, 2)
        for i in range(5000)
        for p in ("a", "b")
    ]
    svg = str(render_test_flow(test, calls))
    assert len(svg) < 200_000
    height = float(re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', svg).group(1))  # type: ignore[union-attr]
    assert height < 4000
    assert f"+{5000 - MAX_FLOW_FUNCTIONS} more functions" in svg and "data-flow-capped" in svg
    assert svg.count('class="fl-link"') <= 1 + MAX_FLOW_FUNCTIONS + 2 * MAX_FLOW_FUNCTIONS
    wide = FlowNode("w", "wide")
    many = [TestCall(wide, f"p{i}", None, 20 - i, 1) for i in range(MAX_FLOW_PARAMS + 8)]
    svg = str(render_test_flow(test, many))
    assert "+8 more parameters" in svg and ">p0 · 1 class<" in svg and "p19" not in svg
    assert "data-flow-capped" not in svg


# -- labels, ids and bidirectional text ------------------------------------------------------------


def test_parametrize_ids_may_hold_double_colons() -> None:
    assert testpage.split_nodeid("t.py::Cls::test_x[a::b]") == ("t.py", ["Cls", "test_x"], "[a::b]")
    assert testpage.test_label("t.py::test_x[a::b]") == "test_x[a::b]"
    assert testpage.test_label("//evil.example") == "//evil.example"
    found = testpage.test_decorations(
        "import pytest\n@pytest.mark.slow\ndef test_x(a):\n  pass\n", "t.py::test_x[a::b]"
    )
    assert found is not None and found.markers == ("slow",)


async def test_huge_ids_are_not_found(client: Client, demo: Demo) -> None:
    rid = demo.repo_id
    assert (await client.get(f"/repos/{rid}/tests/99999999999999999999")).status_code == 404
    assert (await client.get("/repos/99999999999999999999/tests/1")).status_code == 404
    huge = await client.get("/repos/99999999999999999999/tests/by-nodeid?nodeid=t.py::t")
    assert huge.status_code == 404


BIDI_NODEID = "tests/test_refund.py::test_bidi[\u202eevil\u2066x\u2069]"


@pytest.fixture
def bidi_test(demo: Demo) -> Iterator[int]:
    """A test with bidi controls in its nodeid and a result in the baseline run only."""
    con = _db(demo)
    try:
        cur = con.execute(
            "INSERT INTO tests (repo_id, nodeid, nodeid_hash, family, quarantined) VALUES (?, ?, ?, ?, 0)",
            (demo.repo_id, BIDI_NODEID, nodeid_hash(BIDI_NODEID), BIDI_NODEID.split("[", 1)[0]),
        )
        test_id = int(cur.lastrowid or 0)
        con.execute(
            "INSERT INTO test_results (run_id, test_id, outcome, duration_ms, attempts, flaky, quarantined) "
            "VALUES (?, ?, 'passed', 1.0, 1, 0, 0)",
            (demo.baseline_run, test_id),
        )
        con.commit()
        yield test_id
    finally:
        con.execute("DELETE FROM test_results WHERE test_id = ?", (test_id,))
        con.execute("DELETE FROM tests WHERE id = ?", (test_id,))
        con.commit()
        con.close()


async def test_bidi_controls_are_shown_not_applied(client: Client, demo: Demo, bidi_test: int) -> None:
    rid = demo.repo_id
    page = await (await client.get(f"/repos/{rid}/tests/{bidi_test}?run={demo.baseline_run}")).get_data(
        as_text=True
    )
    assert "\u202e" not in page and "\u2066" not in page
    assert "&lt;U+202E&gt;evil" in page
    path = "src/ledger/billing/refund.py"
    lens = await (
        await client.get(f"/repos/{rid}/files/{path}?run={demo.pr1_run}&test={bidi_test}")
    ).get_data(as_text=True)
    assert "\u202e" not in lens and "&lt;U+202E&gt;" in lens
    assert "data-not-in-run" in lens and "not in this run" in lens  # no result in PR #1's run
    here = await (
        await client.get(f"/repos/{rid}/files/{path}?run={demo.baseline_run}&test={bidi_test}")
    ).get_data(as_text=True)
    assert "data-not-in-run" not in here
    test = FlowNode("t", "test_\u202eevil")
    svg = str(render_test_flow(test, [TestCall(FlowNode("f", "f\u202e"), "p\u2066", None, 1, 1)]))
    assert "\u202e" not in svg and "\u2066" not in svg and "&lt;U+202E&gt;" in svg


async def test_prev_and_next_file_keep_the_lens(client: Client, demo: Demo) -> None:
    rid = demo.repo_id
    url = await _failing_test_url(client, demo)
    test_id = url.split("/tests/", 1)[1].split("?", 1)[0]
    page = await (
        await client.get(f"/repos/{rid}/files/src/ledger/billing/refund.py?run={demo.pr1_run}&test={test_id}")
    ).get_data(as_text=True)
    navs = re.findall(r'<a href="([^"]+)" rel="(?:prev|next)"', page)
    assert navs and all(f"test={test_id}" in html_lib.unescape(n) for n in navs)
