"""The history views on the demo: the repo page's overview, the dashboard's trend column, the
function page's history strip, and the sparkline renderer (geometry, keys, escaping)."""

from __future__ import annotations

import html as html_lib
import math
import re
from datetime import UTC, datetime, timedelta

import pytest
from markupsafe import Markup
from quart.typing import TestClientProtocol as Client
from web_helpers import HX, Demo, assert_csp_clean

from coverset.facts import queries
from coverset.facts.queries import FunctionPoint
from coverset.facts.store import FactsStore
from coverset.web import files, repos
from coverset.web.facts_view import history_losses
from coverset.web.pages import pct
from coverset.web.render.sparkline import SparkSeries, fmt_pct, render_spark_key, render_sparkline

SCRIPT = "<script>alert(1)</script>"
BREAKOUT = '"onmouseover="alert(1)'
JS_URL = "javascript:alert(1)"
EVIL = f"{SCRIPT}{BREAKOUT}{JS_URL}"
TAG_WITH_HANDLER = re.compile(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", re.IGNORECASE)


def _store(demo: Demo) -> FactsStore:
    return demo.facts()


def _validate_email_key(demo: Demo) -> str:
    rows = _store(demo).read_table(demo.repo_id, demo.baseline_run, "functions").to_pylist()
    return next(r["func_key"] for r in rows if r["qualname"] == "validate_email")


def _section(page: str, start: str, end: str) -> str:
    a = page.index(start)
    return page[a : page.index(end, a)]


# -- the repository overview ------------------------------------------------------------------------


async def test_repo_overview_tiles_equal_the_facts(client: Client, demo: Demo) -> None:
    response = await client.get(f"/repos/{demo.repo_id}")
    assert response.status_code == 200
    page = await response.get_data(as_text=True)
    assert_csp_clean(page)
    overview = _section(page, 'id="overview"', "<h2>Pull requests</h2>")
    trend = queries.baseline_trend(_store(demo), demo.repo_id, "main")
    latest = trend[-1]
    assert latest.run_id == demo.baseline_run
    tiles = dict(re.findall(r'data-tile="(\w+)"><div class="value">(.*?)</div>', overview))
    assert tiles["statements"] == str(latest.statements)
    assert tiles["lines"] == html_lib.escape(pct(latest.line_pct))
    assert tiles["branches"] == html_lib.escape(pct(latest.branch_pct))
    assert (
        tiles["functions"] == f'{latest.functions_called} <span class="of">/ {latest.functions_total}</span>'
    )
    assert (
        tiles["classes"] == f'{latest.classes_observed} <span class="of">/ {latest.classes_expected}</span>'
    )
    assert tiles["tests"] == str(latest.tests_total)
    assert f"lines run · {latest.lines_run} of {latest.statements}" in overview
    # The overview comes before the PR list and the run list.
    assert (
        page.index('id="overview"') < page.index("<h2>Pull requests</h2>") < page.index("Default-branch runs")
    )


async def test_repo_overview_trend_has_the_six_baselines(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/repos/{demo.repo_id}")).get_data(as_text=True)
    overview = _section(page, 'id="overview"', "<h2>Pull requests</h2>")
    assert 'data-points="6"' in overview
    assert re.search(
        r"6 baselines (over \d+ min|within a minute), newest \d{4}-\d\d-\d\d \d\d:\d\d UTC", overview
    )
    sparks = re.findall(r'<svg class="spark"[^>]*aria-label="([^"]*)"', overview)
    assert len(sparks) == 2
    assert all("over 6 baselines" in label for label in sparks)
    assert sparks[0].startswith("main baselines: lines run from ")
    assert sparks[1].startswith("main baselines: input classes from ")
    # Six hover targets per sparkline, each titled with the run and the value.
    assert overview.count('class="spark-hit"') == 12
    assert overview.count('class="spark-line spark-s2"') == 1  # input classes: dashed


async def test_repo_overview_thumbnail_links_to_the_map(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/repos/{demo.repo_id}")).get_data(as_text=True)
    thumb = _section(page, 'class="ov-thumb"', "</svg>")
    assert '<svg class="treemap mode-gap" viewBox="0 0 520 220"' in thumb
    hrefs = re.findall(r'<a href="([^"]+)"', thumb)
    assert hrefs and all(h.startswith(f"/repos/{demo.repo_id}/map?") for h in hrefs)
    assert all(f"run={demo.baseline_run}" in html_lib.unescape(h) for h in hrefs)
    assert f'href="/repos/{demo.repo_id}/map?run={demo.baseline_run}" class="ov-map-link"' in page
    assert 'class="legend"' in _section(page, 'class="ov-map"', "Largest gaps")


async def test_repo_overview_class_changes_since_the_previous_baseline(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/repos/{demo.repo_id}")).get_data(as_text=True)
    section = _section(page, "Since the previous baseline</h3>", "Largest gaps")
    trend = queries.baseline_trend(_store(demo), demo.repo_id, "main")
    changes = queries.class_changes(_store(demo), demo.repo_id, trend[-1].run_id, trend[-2].run_id)
    assert f'data-changes="{len(changes)}"' in section
    item = next(li for li in section.split("<li>") if ">merge_intervals</code>" in li)
    assert "delta-gained" in item and ">intervals:single</span>" in item
    assert "delta-lost" not in item
    for change in changes:
        assert f">{change.qualname}</code></a>" in section
        assert f"/functions/{change.func_key}?run={trend[-1].run_id}" in section
    assert f"from <code>{trend[-2].head_sha[:8]}</code>" in section


async def test_repo_overview_empty_states(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repos, "class_changes", lambda *args, **kwargs: [])
    page = await (await client.get(f"/repos/{demo.repo_id}")).get_data(as_text=True)
    assert "No input class lost or gained and no body line newly uncovered since" in page
    assert 'data-changes="none"' in page
    monkeypatch.setattr(repos, "baseline_trend", lambda *args, **kwargs: [])
    page = await (await client.get(f"/repos/{demo.repo_id}")).get_data(as_text=True)
    assert "No finished baseline of <code>main</code> yet." in page
    assert 'class="ov-thumb"' not in page
    assert "<h2>Pull requests</h2>" in page
    assert_csp_clean(page)


async def test_repo_overview_largest_gaps_link_to_functions(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/repos/{demo.repo_id}")).get_data(as_text=True)
    table = _section(page, 'class="ov-gaps"', "</table>")
    gaps = queries.top_gaps(_store(demo), demo.repo_id, demo.baseline_run, repos.TOP_GAPS)
    rows = table.split("<tr>")[2:]
    assert len(rows) == len(gaps) == repos.TOP_GAPS
    for row, gap in zip(rows, gaps, strict=True):
        assert f'/repos/{demo.repo_id}/functions/{gap.func_key}?run={demo.baseline_run}"' in row
        assert f"{gap.missing} <span" in row


async def test_overview_and_dashboard_render_as_fragments(client: Client, demo: Demo) -> None:
    for url in (f"/repos/{demo.repo_id}", "/"):
        body = await (await client.get(url, headers=HX)).get_data(as_text=True)
        assert "<!doctype" not in body.lower()
        assert_csp_clean(body)
        assert '<svg class="spark"' in body


async def test_dashboard_rows_show_the_latest_baseline_trend(client: Client, demo: Demo) -> None:
    page = await (await client.get("/")).get_data(as_text=True)
    assert_csp_clean(page)
    cell = _section(page, 'class="dash-trend"', "</td>")
    latest = queries.baseline_trend(_store(demo), demo.repo_id, "main")[-1]
    assert cell.count('<svg class="spark"') == 2
    assert f">{html_lib.escape(fmt_pct(latest.line_pct))}</text>" in cell
    assert f">{html_lib.escape(fmt_pct(latest.input_pct))}</text>" in cell
    assert "over 6 baselines" in cell
    # Other pages do not pay for the trend.
    assert 'class="dash-trend"' not in await (await client.get(f"/repos/{demo.repo_id}/tests")).get_data(
        as_text=True
    )


# -- the function page's history -----------------------------------------------------------------


async def test_function_history_of_validate_email(client: Client, demo: Demo) -> None:
    key = _validate_email_key(demo)
    response = await client.get(f"/repos/{demo.repo_id}/functions/{key}")
    assert response.status_code == 200
    page = await response.get_data(as_text=True)
    assert_csp_clean(page)
    history = _section(page, 'id="history"', "</section>")
    assert 'data-points="6"' in history
    squares = re.findall(r'<li class="(fh-[a-z]+)( is-current)?', history)
    assert [s for s, _ in squares] == ["fh-absent"] * 3 + ["fh-some"] * 3
    assert [bool(c) for _, c in squares] == [False] * 5 + [True]
    assert 'aria-current="page"' in history
    assert history.count('<span aria-hidden="true">8</span>') == 3
    assert "8 of 10 input classes · lines 100%" in history
    assert "function not in this run" in history
    assert "fn-lost" not in history  # nothing was lost since the previous baseline
    # The page sits before the shape grid's panel.
    assert page.index('id="history"') < page.index('class="panel detail"')


async def test_function_history_of_a_pr_run_appends_that_run(client: Client, demo: Demo) -> None:
    key = _validate_email_key(demo)
    page = await (await client.get(f"/repos/{demo.repo_id}/functions/{key}?run={demo.pr2_run}")).get_data(
        as_text=True
    )
    history = _section(page, 'id="history"', "</section>")
    assert 'data-points="7"' in history
    assert history.count("fh-extra") == 1 and "is-current fh-extra" in history
    assert "the last square is this run" in history
    assert "this run (pr-2-subdomain-signups)" in history
    assert 'class="spark-current"' in history


async def test_function_history_names_a_lost_class(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the previous baseline did not miss ``email:idn`` and the newest does, the strip names
    that run, worded as inferred from the stored gaps."""
    real = queries.function_history

    def fake(
        store: FactsStore, repo_id: int, func_key: str, branch: str, last_n: int = 12, **kwargs: object
    ) -> list[FunctionPoint]:
        points = real(store, repo_id, func_key, branch, last_n, **kwargs)  # type: ignore[arg-type]
        previous = points[-2]
        points[-2] = FunctionPoint(
            previous.run_id,
            previous.created_at,
            previous.head_sha,
            True,
            previous.expected,
            (previous.observed or 0) + 1,
            previous.line_pct,
            previous.tests_called,
            tuple(m for m in previous.missing if m != "email:idn"),
            sig_hash=previous.sig_hash,
        )
        return points

    monkeypatch.setattr(files, "function_history", fake)
    key = _validate_email_key(demo)
    page = await (await client.get(f"/repos/{demo.repo_id}/functions/{key}")).get_data(as_text=True)
    lost = _section(page, 'class="fn-lost"', "</ul>")
    trend = queries.baseline_trend(_store(demo), demo.repo_id, "main")
    assert '<span class="chip chip-hollow">email:idn</span> missing now, not missing in run' in lost
    assert f"<code>{trend[-2].head_sha[:8]}</code>" in lost and "not since" not in lost
    assert "email:multi-label-domain" not in lost
    assert "Inferred from the stored gaps" in _section(page, 'class="fn-lost"', "</section>")


def test_trend_span_wording() -> None:
    t0 = datetime(2026, 10, 12, 1, 47, tzinfo=UTC)
    assert repos.trend_span(t0, t0 + timedelta(minutes=3), 6) == "over 3 min, newest 2026-10-12 01:50 UTC"
    assert (
        repos.trend_span(t0, t0 + timedelta(seconds=20), 6) == "within a minute, newest 2026-10-12 01:47 UTC"
    )
    assert repos.trend_span(t0, t0 + timedelta(hours=5), 3) == "over 5 h, newest 2026-10-12 06:47 UTC"
    assert repos.trend_span(t0 - timedelta(days=11), t0, 12) == "2026-10-01 → 2026-10-12"
    assert repos.trend_span(t0, t0, 1) == "2026-10-12 01:47 UTC"


def _point(
    i: int, *, expected: int | None = 4, missing: tuple[str, ...] = (), present: bool = True
) -> FunctionPoint:
    observed = None if expected is None else expected - len(missing)
    return FunctionPoint(
        f"run{i}",
        datetime(2026, 10, i + 1, tzinfo=UTC),
        f"{i:040x}",
        present,
        expected,
        observed,
        100.0,
        3,
        missing,
    )


def test_history_losses() -> None:
    points = [
        _point(0, missing=("a:x", "a:y")),
        _point(1, missing=("a:y",)),
        _point(2, missing=("a:y",)),
        _point(3, missing=("a:x", "a:y")),
    ]
    lost = history_losses(points, 3)
    assert [(c.name, c.last_seen.run_id) for c in lost] == [("a:x", "run2")]
    assert history_losses(points, 2) == []
    assert history_losses(points, 0) == []
    assert history_losses(points, 9) == []
    # A different expected count says nothing about what was observed.
    assert history_losses([_point(0, expected=5), _point(1, missing=("a:x",))], 1) == []
    assert history_losses([_point(0), _point(1, expected=None)], 1) == []


# -- the sparkline renderer -------------------------------------------------------------------------


def test_sparkline_geometry_labels_and_titles() -> None:
    svg = str(
        render_sparkline(
            [SparkSeries("lines", (80.0, 85.0, None, 90.0)), SparkSeries("inputs", (30.0, 31.0, 32.0, 33.5))],
            points=["a", "b", "c", "d"],
            width=200,
            label="main",
            noun="baseline",
        )
    )
    assert svg.startswith('<svg class="spark" viewBox="0 0 200 36" width="200" height="36" role="img"')
    assert 'aria-label="main: lines from 80% to 90%; inputs from 30% to 33.5% over 4 baselines"' in svg
    assert svg.count('class="spark-hit"') == 4
    assert "<title>c · lines n/a · inputs 32%</title>" in svg
    # The gap splits the first series: a line through the first two values, a dot for the last.
    assert svg.count('class="spark-line spark-s1"') == 1 and svg.count('class="spark-dot spark-s1"') == 1
    assert svg.count('class="spark-line spark-s2"') == 1  # the second series is dashed
    assert ">90%</text>" in svg and ">33.5%</text>" in svg
    ys = [float(y) for y in re.findall(r'<text class="spark-val" x="[^"]+" y="([^"]+)"', svg)]
    assert len(ys) == 2 and abs(ys[0] - ys[1]) >= 10 - 1e-9
    numbers = [float(n) for n in re.findall(r'(?:x|y|x1|x2|y1|y2|cx|cy|width)="([-0-9.]+)"', svg)]
    assert all(math.isfinite(n) and -1 <= n <= 200 for n in numbers)


def test_sparkline_edges() -> None:
    one = str(render_sparkline([SparkSeries("x", (50.0,))], points=["only"], width=100))
    assert 'width="160"' in one  # clamped to the smallest width
    assert "x 50% over 1 run" in one
    wide = str(render_sparkline([SparkSeries("x", (1.0, 2.0))], points=["a", "b"], width=999))
    assert 'width="240"' in wide
    empty = str(render_sparkline([SparkSeries("x", (None, float("nan")))], points=["a", "b"]))
    assert "x: no value over 2 runs" in empty and "nan" not in empty.lower()
    ref = str(
        render_sparkline(
            [SparkSeries("x", (10.0, 20.0))],
            points=["a", "b"],
            reference=15.0,
            reference_label="base",
            current=1,
        )
    )
    assert "<title>base: 15%</title>" in ref and 'class="spark-current"' in ref
    single_dashed = str(render_sparkline([SparkSeries("x", (1.0, 2.0), dashed=True)], points=["a", "b"]))
    assert "spark-line spark-s2" in single_dashed
    both_dashed = str(
        render_sparkline(
            [SparkSeries("x", (1.0, 2.0), dashed=True), SparkSeries("y", (3.0, 4.0), dashed=True)],
            points=["a", "b"],
        )
    )
    assert both_dashed.count("spark-line spark-s1") == 1 and both_dashed.count("spark-line spark-s2") == 1
    with pytest.raises(ValueError):
        render_sparkline([], points=[])
    with pytest.raises(ValueError):
        render_sparkline([SparkSeries(str(i), (1.0,)) for i in range(3)], points=["a"])
    with pytest.raises(ValueError):
        render_sparkline([SparkSeries("x", (1.0, 2.0))], points=["a"])


@pytest.mark.security
def test_sparkline_escapes_hostile_labels() -> None:
    svg = render_sparkline(
        [SparkSeries(EVIL, (10.0, 20.0)), SparkSeries(EVIL, (5.0, None))],
        points=[EVIL, EVIL],
        label=EVIL,
        noun=EVIL,
        reference=12.0,
        reference_label=EVIL,
    )
    key = render_spark_key([SparkSeries(EVIL, ()), SparkSeries(EVIL, (), dashed=True)])
    for html in (svg, key):
        assert isinstance(html, Markup)
        text = str(html)
        assert "<script" not in text.lower()
        assert not TAG_WITH_HANDLER.search(text), TAG_WITH_HANDLER.search(text)
        assert '"onmouseover="' not in text
        assert "&lt;script&gt;" in text


# -- review fixes ---------------------------------------------------------------------------------


async def test_function_history_is_anchored_at_an_old_baseline(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Viewing a baseline older than the window shows the baselines up to it, it last: the strip
    never runs backwards (a 2-run window over the demo's six baselines)."""
    monkeypatch.setattr(files, "HISTORY_RUNS", 2)
    key = _validate_email_key(demo)
    trend = queries.baseline_trend(_store(demo), demo.repo_id, "main")
    old = trend[-3]
    page = await (await client.get(f"/repos/{demo.repo_id}/functions/{key}?run={old.run_id}")).get_data(
        as_text=True
    )
    history = _section(page, 'id="history"', "</section>")
    assert 'data-points="2"' in history and "fh-extra" not in history
    squares = re.findall(r'<li class="fh-[a-z]+( is-current)?">.*?title="baseline ([0-9a-f]{8})', history)
    assert squares == [("", trend[-4].head_sha[:8]), (" is-current", old.head_sha[:8])]
    assert "2 baselines of <code>main</code> up to this one" in history


async def test_function_history_of_a_pr_run_keeps_the_sample_cap(client: Client, demo: Demo) -> None:
    """The PR run's point reads ``tests_truncated`` from its stored row, as a baseline's does."""
    from coverset.web.facts_view import run_facts, run_point

    facts = run_facts(_store(demo), demo.repo_id, demo.pr2_run)
    capped = [k for k, row in facts.input_cov_rows.items() if (row.get("tests_truncated") or 0) > 0]
    assert capped, "the demo's sample cap leaves some function capped in PR #2"
    region = facts.region(capped[0])
    assert region is not None
    point = run_point(facts, region, None, "x" * 40)
    assert point.tests_truncated == facts.input_cov_rows[capped[0]]["tests_truncated"] > 0
    assert point.sig_hash == region.sig_hash


async def test_class_and_module_pages_have_no_history(client: Client, demo: Demo) -> None:
    rows = _store(demo).read_table(demo.repo_id, demo.baseline_run, "functions").to_pylist()
    for kind in ("class", "module"):
        found = next((r for r in rows if r["kind"] == kind), None)
        if found is None:
            continue
        page = await (await client.get(f"/repos/{demo.repo_id}/functions/{found['func_key']}")).get_data(
            as_text=True
        )
        assert 'id="history"' not in page, kind


async def test_repo_overview_words_class_changes_as_inferred(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    trend = queries.baseline_trend(_store(demo), demo.repo_id, "main")
    real = queries.class_changes(_store(demo), demo.repo_id, trend[-1].run_id, trend[-2].run_id)
    import dataclasses

    named = dataclasses.replace(real[0], lost=("x:lost",), lost_count=1, gained=("x:gained",), gained_count=1)
    moved = dataclasses.replace(
        real[0],
        lost=(),
        gained=(),
        lost_count=0,
        gained_count=0,
        expected=7,
        prior_expected=5,
        uncompared=True,
    )
    resigned = dataclasses.replace(moved, expected=5, sig_changed=True)
    monkeypatch.setattr(repos, "class_changes", lambda *a, **k: [named, moved, resigned])
    page = await (await client.get(f"/repos/{demo.repo_id}")).get_data(as_text=True)
    section = _section(page, "Since the previous baseline</h3>", "Largest gaps")
    assert "observed in" not in section
    assert f"x:lost: missing in {trend[-1].head_sha[:8]}, not missing in {trend[-2].head_sha[:8]}" in section
    assert ">missing now</span>" in section and "inferred from the stored gaps" in section
    assert "expects 5 → 7 classes: classes not compared" in section
    assert "its signature changed: classes not compared" in section


async def test_repo_overview_delta_is_na_without_a_percentage(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    real = queries.baseline_trend

    def trend(*args: object, **kwargs: object) -> list[object]:
        points = real(*args, **kwargs)  # type: ignore[arg-type]
        points[-2] = dataclasses.replace(points[-2], statements=0, lines_run=0, classes_expected=0)
        return points

    monkeypatch.setattr(repos, "baseline_trend", trend)
    page = await (await client.get(f"/repos/{demo.repo_id}")).get_data(as_text=True)
    delta = _section(page, 'class="muted ov-delta"', "</p>")
    assert "lines run n/a · input classes n/a" in delta
    assert "pts" not in delta.split("(")[0]


def test_thumbnail_tree_merges_tiny_tiles() -> None:
    from coverset.web.render.treemap import Rect, TreeItem, layout

    def fn(file: str, i: int, statements: int, cov: float | None) -> TreeItem:
        return TreeItem(
            f"f{i}", f"{file}::f{i}", "function", statements, statements, cov, f"k{file}{i}", i, file
        )

    big = TreeItem("big.py", "pkg/big.py", "file", 4000, 4000, children=[fn("pkg/big.py", 0, 4000, 0.5)])
    # 2,000 files of 3 statements (about 34 px² each), 3 functions apiece; one file of 10 small
    # functions next to a large one
    many = [
        TreeItem(
            f"m{j}.py",
            f"pkg/m{j}.py",
            "file",
            3,
            2,
            children=[fn(f"pkg/m{j}.py", i, 1, 0.25) for i in range(3)],
        )
        for j in range(2000)
    ]
    mixed = TreeItem(
        "mixed.py",
        "pkg/mixed.py",
        "file",
        2010,
        2010,
        children=[fn("pkg/mixed.py", 99, 2000, 1.0), *(fn("pkg/mixed.py", i, 1, None) for i in range(10))],
    )
    tree = TreeItem("pkg", "pkg", "dir", 12010, 8010, children=[big, mixed, *many])
    thumb = repos.thumbnail_tree(tree, 520, 220)
    leaves = [x for x in layout(thumb, Rect(0, 0, 520, 220)) if not x.item.children]
    assert len(leaves) == 4 and sum(x.item.statements for x in leaves) == 4000 + 2010 + 2000 * 3
    files_tile = next(x.item for x in leaves if x.item.name == "2000 small files")
    assert files_tile.file == "pkg" and files_tile.covered == 4000
    assert files_tile.input_cov == pytest.approx(0.25)
    fns_tile = next(x.item for x in leaves if x.item.name == "10 small functions")
    assert fns_tile.file == "pkg/mixed.py" and fns_tile.input_cov is None and fns_tile.statements == 10
    # a tree whose tiles are all large enough is returned as it was
    demo_like = TreeItem("pkg", "pkg", "dir", 4000, 4000, children=[big])
    assert repos.thumbnail_tree(demo_like, 520, 220) == demo_like


async def test_repo_overview_thumbnail_keeps_the_demo_tiles(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/repos/{demo.repo_id}")).get_data(as_text=True)
    thumb = _section(page, 'class="ov-thumb"', "</svg>")
    assert "small functions" not in thumb and "small files" not in thumb
    assert thumb.count('class="tm-leaf ') > 20


def test_sparkline_clamps_values_and_strips_control_characters() -> None:
    import xml.dom.minidom

    svg = str(
        render_sparkline(
            [
                SparkSeries("a\x01b", (-1e300, 1e300, 50.0)),
                SparkSeries("c", (1e308, None, -5.0), dashed=True),
            ],
            points=["p\x00", "q\x1b", "r\nend"],
            label="l\x07",
            noun="run\x08",
            reference=1e300,
            reference_label="ref\x02",
        )
    )
    xml.dom.minidom.parseString(svg)  # well-formed
    assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", svg)
    assert "r end" in svg and "<title>ref: 100%</title>" in svg
    assert "ab from 0% to 50%" in svg
    numbers = [float(n) for n in re.findall(r'(?:x|y|x1|x2|y1|y2|cx|cy|width)="([-0-9.e+]+)"', svg)]
    assert all(math.isfinite(n) and -1 <= n <= 240 for n in numbers)
    key = str(render_spark_key([SparkSeries("k\x03", ())]))
    assert "\x03" not in key


def test_no_global_rule_restyles_changed_source_lines() -> None:
    """The source view marks changed lines ``.ln.chg``; a bare ``.chg`` rule (as the overview's
    labels once had) restyles every changed line of every source page."""
    from pathlib import Path

    css = (Path(repos.__file__).parent / "static" / "app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selectors in re.findall(r"([^{}]+)\{", css):
        for selector in selectors.split(","):
            if re.search(r"\.chg(?![\w-])", selector):
                assert re.search(r"\.ln\.chg(?![\w-])", selector), selector.strip()
    # and the rule this guards against is caught: the old overview label style
    old = ".chg { margin-left: 6px; }"
    bad = [s for s in re.findall(r"([^{}]+)\{", old) if not re.search(r"\.ln\.chg(?![\w-])", s)]
    assert bad == [".chg "]
