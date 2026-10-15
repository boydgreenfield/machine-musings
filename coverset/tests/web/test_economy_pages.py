"""The Economy pages, the leverage-table fragments, the deselect download and the PR page's Test
tax card, on the real demo data (whose runs stored their economy at ``finalize_run``)."""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any
from urllib.parse import unquote_plus

import pytest
from quart import Quart
from quart.typing import TestClientProtocol as Client
from web_helpers import HX, Demo, assert_csp_clean

from coverset.report.economy import FILTERS, deselect_args, view_from_store
from coverset.web.economy import DEFAULT_LIMIT, TABS, clear_cache


@pytest.fixture
def settings_overrides(demo: Demo, admin_token: str, request: pytest.FixtureRequest) -> dict[str, Any]:
    """The demo data; tests that sign a user in (they request ``signed_in``) also set an admin
    token, so the instance is no longer in open mode."""
    values: dict[str, Any] = demo.overrides()
    if "signed_in" in request.fixturenames:
        values["admin_token"] = admin_token
    return values


@pytest.fixture
def signed_in() -> None:
    """Marker fixture: see ``settings_overrides``."""


@pytest.fixture(autouse=True)
def _fresh_views() -> None:
    clear_cache()


def _view(demo: Demo, run_id: str) -> Any:
    view = view_from_store(demo.facts(), demo.repo_id, run_id)
    assert view is not None and view.totals is not None
    return view


def _rows(body: str) -> list[str]:
    """Node ids of the leverage table's rows, in order."""
    table = body[body.index('id="eco-tests"') :]
    return [
        unquote_plus(html_lib.unescape(n))
        for n in re.findall(
            r'<tr(?: class="is-removable")?>\s*<td class="nodeid"><a [^>]*href="[^"]*nodeid=([^"&]+)', table
        )
    ]


async def _get(client: Client, url: str, **kwargs: Any) -> str:
    response = await client.get(url, **kwargs)
    assert response.status_code == 200, url
    return await response.get_data(as_text=True)


async def test_every_tab_renders_the_stored_economy(client: Client, demo: Demo) -> None:
    view = _view(demo, demo.pr1_run)
    t = view.totals
    base = f"/repos/{demo.repo_id}/economy?run={demo.pr1_run}"
    overview = await _get(client, base)
    assert_csp_clean(overview)
    assert 'aria-current="page">Economy</a>' in overview  # the nav partial, current
    assert 'data-go="e"' in overview  # the g e key reaches it
    tile = re.search(r'data-tile="minimal"[^>]*><div class="value">(\d+) <span class="of">/ (\d+)', overview)
    assert tile and tile.groups() == (str(t.minimal_set_size), str(t.tests))
    assert f'<div class="value">{t.removable_candidates}</div>' in overview
    assert '<svg class="eco-hist"' in overview and 'class="eco-bin eco-zero"' in overview
    assert 'id="test-tax"' in overview  # PR #1 compares with its base run
    assert "Biggest tests that add nothing alone" in overview
    for key, _label in TABS[1:]:
        body = await _get(client, f"{base}&tab={key}")
        assert_csp_clean(body)
        assert (
            f'href="/repos/{demo.repo_id}/economy?run={demo.pr1_run}&amp;tab={key}" aria-current="true"'
            in body
        )
    redundant = await _get(client, f"{base}&tab=redundant")
    assert "Candidates, not verdicts." in redundant
    assert f"<strong>Keeps {t.minimal_set_size}</strong>" in redundant
    assert redundant.count('class="eco-group"') == t.equivalence_groups
    assert f"/repos/{demo.repo_id}/economy/deselect.txt?run={demo.pr1_run}" in redundant
    families = await _get(client, f"{base}&tab=families")
    assert families.count('class="is-collapsible"') == t.collapsible_families
    functions = await _get(client, f"{base}&tab=functions")
    assert f"{t.untested_functions} of {t.functions}" in functions
    assert "Exception handlers no test ran" in functions


async def test_the_default_run_is_the_newest_baseline_without_a_tax(client: Client, demo: Demo) -> None:
    body = await _get(client, f"/repos/{demo.repo_id}/economy")
    assert 'id="eco-tiles"' in body and 'id="test-tax"' not in body
    assert "PR test tax not computed: no base run" in body  # the "not measured" strip


async def test_sorting_filters_and_the_fragment(client: Client, demo: Demo) -> None:
    view = _view(demo, demo.pr1_run)
    url = f"/repos/{demo.repo_id}/economy?run={demo.pr1_run}&tab=leverage"
    page = await _get(client, url)
    rows = _rows(page)
    assert len(rows) == min(DEFAULT_LIMIT, len(view.tests))
    by_id = view.by_nodeid()
    levers = [by_id[n].leverage for n in rows if by_id[n].leverage is not None]
    assert levers == sorted(levers)  # lowest leverage first
    # the header links: a full-page href, plus the fragment for htmx that pushes the page URL
    header = re.search(r'<a class="eco-sort" href="([^"]+sort=statements[^"]*)" hx-get="([^"]+)"', page)
    assert header and "/economy/tests?" in header.group(2)
    fragment = await _get(client, html_lib.unescape(header.group(2)), headers=HX)
    assert "<!doctype" not in fragment.lower() and fragment.lstrip().startswith(
        '<section class="panel eco-table"'
    )
    assert_csp_clean(fragment)
    ordered = _rows(fragment)
    sizes = [by_id[n].statements for n in ordered]
    assert sizes == sorted(sizes, reverse=True)  # most statements first
    assert 'aria-sort="descending"' in fragment
    flipped = _rows(
        await _get(client, f"/repos/{demo.repo_id}/economy/tests?run={demo.pr1_run}&sort=statements&dir=rev")
    )
    assert [by_id[n].statements for n in flipped] == sorted(sizes)
    for only, (_label, keep) in FILTERS.items():
        body = await _get(client, f"{url}&only={only}&limit=all")
        assert all(keep(by_id[n]) for n in _rows(body)), only
        assert len(_rows(body)) == sum(1 for r in view.tests if keep(r))
    capped = await _get(client, f"{url}&limit=5")
    assert len(_rows(capped)) == 5 and "Show all" in capped
    junk = await _get(client, f"{url}&sort=<script>&only=nope&limit=-3&dir=x")
    assert len(_rows(junk)) == min(DEFAULT_LIMIT, len(view.tests))  # bad values fall back to defaults


async def test_the_deselect_download_is_a_pytest_argument_file(client: Client, demo: Demo) -> None:
    view = _view(demo, demo.pr1_run)
    response = await client.get(f"/repos/{demo.repo_id}/economy/deselect.txt?run={demo.pr1_run}")
    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    assert "attachment" in response.headers["Content-Disposition"]
    body = await response.get_data(as_text=True)
    assert body == deselect_args(view)
    lines = body.splitlines()
    assert len(lines) == view.totals.removable_candidates
    assert all(line.startswith("--deselect=tests/") for line in lines)


async def test_unknown_repos_and_runs_are_404(client: Client, demo: Demo) -> None:
    for url in (
        "/repos/999/economy",
        "/repos/999/economy/tests",
        "/repos/999/economy/deselect.txt",
        "/repos/999/economy/pr-tax",
        f"/repos/{demo.repo_id}/economy?run=run_00000000000000000000000000",
        f"/repos/{demo.repo_id}/economy?run=../../etc",
    ):
        assert (await client.get(url)).status_code == 404, url


async def test_a_member_without_access_gets_404_and_a_reader_gets_the_page(
    app: Quart, client: Client, demo: Demo, signed_in: None
) -> None:
    from test_rbac import sign_in_as

    await sign_in_as(app, client, "outsider", access={})
    for path in ("economy", "economy/tests", "economy/deselect.txt", "economy/pr-tax"):
        response = await client.get(f"/repos/{demo.repo_id}/{path}?run={demo.pr1_run}")
        assert response.status_code == 404, path
    await sign_in_as(app, client, "reader", access={demo.repo_id: "read"})
    assert (await client.get(f"/repos/{demo.repo_id}/economy?run={demo.pr1_run}")).status_code == 200
    assert (
        await client.get(f"/repos/{demo.repo_id}/economy/deselect.txt?run={demo.pr1_run}")
    ).status_code == 200


def _summary(demo: Demo, run_id: str) -> dict[str, Any]:
    return json.loads(demo.rows("SELECT summary_json FROM runs WHERE id = :id", {"id": run_id})[0][0])


def _set_summary(demo: Demo, run_id: str, data: dict[str, Any]) -> None:
    demo.rows("UPDATE runs SET summary_json = :data WHERE id = :id", {"data": json.dumps(data), "id": run_id})


async def test_the_pr_page_shows_the_verdicts_test_tax(client: Client, demo: Demo) -> None:
    stored = _summary(demo, demo.pr1_run)["test_tax"]  # the verdict stored the tax
    view = _view(demo, demo.pr1_run)
    assert stored["added_statements"] == view.tax.added_statements
    assert stored["newly_covered_lines"] == view.tax.newly_covered_lines
    page = await _get(client, f"/repos/{demo.repo_id}/prs/1")
    assert_csp_clean(page)
    card = page[page.index('id="test-tax"') - 60 :]
    card = card[: card.index("</section>")]
    assert 'data-flagged="false"' in card and "within threshold" in card
    assert f"{stored['added_statements']} test statement" in card
    assert "economy/pr-tax" not in card  # rendered from the summary, no second request
    assert "New tests with the lowest leverage" in card


async def test_a_flagged_tax_is_red_and_the_lazy_slot_fills_old_runs(client: Client, demo: Demo) -> None:
    original = _summary(demo, demo.pr1_run)
    try:
        flagged = {
            **original,
            "test_tax": {
                **original["test_tax"],
                "added_statements": 95,
                "newly_covered_lines": 3,
                "new_classes": 0,
                "tax": 31.667,
                "flagged": True,
                "duplicates": [
                    {
                        "nodeid": "tests/test_refund.py::test_refund_partial",
                        "statements": 4,
                        "unique_facts": 0,
                        "leverage": 0.0,
                        "duplicates": "tests/test_allocate.py::test_split_evenly[10-3-expected0]",
                        "how": "same flow",
                    }
                ],
                "duplicate_count": 1,
            },
        }
        _set_summary(demo, demo.pr1_run, flagged)
        page = await _get(client, f"/repos/{demo.repo_id}/prs/1")
        card = page[page.index('id="test-tax"') - 80 :]
        card = card[: card.index("</section>")]
        assert "is-flagged" in card and 'data-flagged="true"' in card
        assert "over threshold" in card and "31.7" in card
        assert "same flow as" in card and "test_split_evenly" in card
        # a summary from before the tax existed: the slot fetches the card from the stored economy
        _set_summary(demo, demo.pr1_run, {k: v for k, v in original.items() if k != "test_tax"})
        page = await _get(client, f"/repos/{demo.repo_id}/prs/1")
        slot = re.search(
            r'<section class="panel eco-tax eco-tax-pending" id="test-tax" hx-get="([^"]+)"', page
        )
        assert slot
        fragment = await _get(client, html_lib.unescape(slot.group(1)), headers=HX)
        assert 'id="test-tax"' in fragment and "<!doctype" not in fragment.lower()
        assert_csp_clean(fragment)
    finally:
        _set_summary(demo, demo.pr1_run, original)


async def test_the_pr_tax_fragment_is_empty_without_a_base(client: Client, demo: Demo) -> None:
    body = await _get(client, f"/repos/{demo.repo_id}/economy/pr-tax?run={demo.baseline_run}")
    assert body == ""


async def test_a_run_without_economy_says_so(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("coverset.report.economy.run_economy", lambda *_args: None)
    body = await _get(client, f"/repos/{demo.repo_id}/economy?run={demo.pr2_run}")
    assert 'id="eco-missing"' in body and "No test economy for this run" in body
    assert (await client.get(f"/repos/{demo.repo_id}/economy/tests?run={demo.pr2_run}")).status_code == 404
    assert (
        await client.get(f"/repos/{demo.repo_id}/economy/deselect.txt?run={demo.pr2_run}")
    ).status_code == 404
