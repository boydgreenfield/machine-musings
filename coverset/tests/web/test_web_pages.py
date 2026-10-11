"""Every UI route renders as a full page and as an htmx fragment against the real demo data."""

from __future__ import annotations

import re

import pytest
from quart.typing import TestClientProtocol as Client
from web_helpers import BOOSTED, HX, Demo, assert_csp_clean

from coverset.app import CONTENT_SECURITY_POLICY


def page_urls(demo: Demo) -> list[str]:
    rid = demo.repo_id
    return [
        "/",
        f"/repos/{rid}",
        f"/repos/{rid}/prs/1",
        f"/repos/{rid}/prs/2",
        f"/repos/{rid}/prs/2?run={demo.pr2_run}",
        f"/runs/{demo.pr1_run}",
        f"/runs/{demo.pr2_run}",
        f"/runs/{demo.baseline_run}",
        f"/runs/{demo.pr1_run}?replay=1&speed=0.25",
        f"/repos/{rid}/map",
        f"/repos/{rid}/map?mode=lines",
        f"/repos/{rid}/map?mode=inputs&path=src/ledger/billing",
        f"/repos/{rid}/map?mode=bogus&path=no/such/dir",
        f"/repos/{rid}/tests",
        f"/repos/{rid}/tests?sort=slow",
        f"/repos/{rid}/invariants",
        f"/repos/{rid}/invariants?run={demo.pr2_run}",
        "/forge/outbox",
        "/forge/outbox?method=upsert_pr_comment",
        f"/repos/{rid}/files/src/ledger/durations/parse.py",
        f"/repos/{rid}/files/src/ledger/accounts/emailcheck.py?pr=2",
        f"/repos/{rid}/files/src/ledger/billing/refund.py?run={demo.pr1_run}",
        f"/repos/{rid}/files/src/ledger/durations/parse.py/lines/16/tests?run={demo.baseline_run}",
    ]


async def test_every_page_renders_full_and_as_fragment(client: Client, demo: Demo) -> None:
    for url in page_urls(demo):
        full = await client.get(url)
        assert full.status_code == 200, url
        html = await full.get_data(as_text=True)
        assert html.lstrip().lower().startswith("<!doctype html>"), url
        assert 'name="htmx-config"' in html and "selfRequestsOnly" in html
        assert full.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        assert "HX-Request" in full.headers["Vary"]
        assert_csp_clean(html)
        fragment = await client.get(url, headers=HX)
        assert fragment.status_code == 200, url
        body = await fragment.get_data(as_text=True)
        assert "<!doctype" not in body.lower() and 'class="top"' not in body, url
        assert_csp_clean(body)
        boosted = await client.get(url, headers=BOOSTED)
        assert (await boosted.get_data(as_text=True)).lstrip().lower().startswith("<!doctype html>"), url


async def test_setup_is_restyled_onto_the_layout(client: Client) -> None:
    html = await (await client.get("/setup")).get_data(as_text=True)
    assert 'class="top"' in html and 'class="setup"' in html and "Sandboxes" in html
    assert_csp_clean(html)


async def test_layout_wiring(client: Client) -> None:
    html = await (await client.get("/")).get_data(as_text=True)
    for script in ("app.js", "vendor/htmx.min.js", "vendor/idiomorph-ext.min.js", "vendor/sse.min.js"):
        assert f"/static/{script}" in html
    assert 'hx-ext="morph,sse"' in html
    token = re.search(r'hx-headers=\'\{"X-CSRF-Token": "([^"]+)"\}\'', html)
    assert token and len(token.group(1)) > 20
    for label in ("Dashboard", "coverset-demo/ledger", "Tests", "Invariants", "Outbox", "Setup"):
        assert f">{label}</a>" in html
    assert 'data-ready="0"' in html


async def test_pr_pages_tell_the_demo_story(client: Client, demo: Demo) -> None:
    pr1 = await (await client.get(f"/repos/{demo.repo_id}/prs/1")).get_data(as_text=True)
    assert "s-fail" in pr1 and ">FAIL<" in pr1
    assert "first failure" in pr1 and "file order: 66th" in pr1 and "est.)" in pr1
    assert ">refund<" in pr1 and ">NEW<" in pr1 and "1 covering test failed" in pr1
    assert "cents_conserved" in pr1 and 'class="inv i-violated"' in pr1
    assert "AI (fixture)" in pr1 and "advisory" in pr1
    pr2 = await (await client.get(f"/repos/{demo.repo_id}/prs/2")).get_data(as_text=True)
    assert "PASS with gaps" in pr2
    assert "AI review: fixtures (advisory)" in pr2
    assert "signup_rejects_existing_domain untested" in pr2
    assert "email:multi-label-domain" in pr2 or "multi-label-domain" in pr2
    assert "sg-hollow" in pr2 and "sg-filled" in pr2
    assert "Suggested Hypothesis test" in pr2 and 'data-copy-target="#suggest-' in pr2
    for section in ("Tests", "Invariants", "Indirect changes", "Invariants changed by this PR"):
        assert f"<summary>{section} " in pr2


async def test_pr_card_and_function_fragments(client: Client, demo: Demo) -> None:
    pr2 = await (await client.get(f"/repos/{demo.repo_id}/prs/2")).get_data(as_text=True)
    keys = list(dict.fromkeys(re.findall(r"/prs/2/functions/([0-9a-f]{16})", pr2)))
    assert keys
    for key in keys:
        card = await client.get(f"/repos/{demo.repo_id}/prs/2/functions/{key}?run={demo.pr2_run}", headers=HX)
        assert card.status_code == 200
        body = await card.get_data(as_text=True)
        assert "shape-grid" in body and "flow-ribbon" in body and "Top covering tests" in body
        page = await client.get(f"/repos/{demo.repo_id}/functions/{key}?run={demo.pr2_run}")
        assert page.status_code == 200
        grid = await client.get(
            f"/repos/{demo.repo_id}/functions/{key}/shapes?run={demo.pr2_run}", headers=HX
        )
        grid_html = await grid.get_data(as_text=True)
        assert grid.status_code == 200 and "Input classes" in grid_html
        cell = re.search(r'hx-get="([^"]+cls=[^"]+)"', grid_html)
        assert cell
        detail = await client.get(cell.group(1).replace("&amp;", "&"), headers=HX)
        assert detail.status_code == 200
        text = await detail.get_data(as_text=True)
        assert "observed in" in text or "Suggested Hypothesis strategy" in text


async def test_hollow_cell_suggests_a_strategy(client: Client, demo: Demo) -> None:
    pr2 = await (await client.get(f"/repos/{demo.repo_id}/prs/2")).get_data(as_text=True)
    url = re.search(r'hx-get="([^"]+cls=email:multi-label-domain[^"]*)"', pr2)
    assert url, "validate_email's multi-label-domain cell"
    body = await (await client.get(url.group(1).replace("&amp;", "&"), headers=HX)).get_data(as_text=True)
    assert "never exercised" in body and "bob@eng.acme.com" in body


async def test_source_view_lines_chips_and_navigation(client: Client, demo: Demo) -> None:
    url = f"/repos/{demo.repo_id}/files/src/ledger/durations/parse.py?run={demo.pr2_run}"
    html = await (await client.get(url)).get_data(as_text=True)
    assert re.search(r'<div class="ln run[^"]*" id="L15"', html)
    assert 'class="fn-region d0"' in html and "fn-head" in html
    assert "multi-letter-unit <b>0</b>" in html and "chip-hollow" in html
    assert 'popovertarget="line-tests"' in html
    assert 'rel="prev" data-key-nav' in html and 'rel="next" data-key-nav' in html
    assert 'id="keys-help"' in html


async def test_source_view_marks_changed_lines_for_a_pr(client: Client, demo: Demo) -> None:
    url = f"/repos/{demo.repo_id}/files/src/ledger/billing/refund.py?pr=1"
    html = await (await client.get(url)).get_data(as_text=True)
    assert html.count('aria-label="changed"') >= 3


async def test_map_modes_and_leaf_links(client: Client, demo: Demo) -> None:
    for mode, cls in (("gap", "tm-g"), ("lines", "tm-b"), ("inputs", "tm-b")):
        html = await (await client.get(f"/repos/{demo.repo_id}/map?mode={mode}")).get_data(as_text=True)
        assert f"mode-{mode}" in html and cls in html
        assert re.search(r'href="/repos/\d+/files/src/ledger/[^"]+#L\d+"', html)
    gap = await (await client.get(f"/repos/{demo.repo_id}/map")).get_data(as_text=True)
    assert "mode-gap" in gap and "tm-hatch" in gap and "Largest gaps" in gap


async def test_run_page_live_regions_and_order(client: Client, demo: Demo) -> None:
    html = await (await client.get(f"/runs/{demo.pr1_run}")).get_data(as_text=True)
    for region in ("run-head", "run-timeline", "run-gantt", "run-failures"):
        assert f'id="{region}"' in html
    assert "sse-connect" not in html  # a finished run is not streamed unless replayed
    assert "ran 1st; file order would have run it 66th" in html
    assert "gt-fail" in html and "gt-pass" in html
    assert "test_refund_partial" in html and "longrepr" in html
    replay = await (await client.get(f"/runs/{demo.pr1_run}?replay=1&speed=0.25&freeze=1")).get_data(
        as_text=True
    )
    assert 'sse-connect="/runs/' in replay and "replay=1" in replay and "freeze=1" in replay
    assert 'sse-close="done"' in replay and 'sse-swap="gantt"' in replay
    assert "No shard has reported a test yet." in replay


async def test_coverage_redirects_to_the_pr_page(client: Client, demo: Demo) -> None:
    response = await client.get(f"/runs/{demo.pr2_run}/coverage")
    assert response.status_code == 302
    assert response.headers["Location"] == f"/repos/{demo.repo_id}/prs/2?run={demo.pr2_run}"
    baseline = await client.get(f"/runs/{demo.baseline_run}/coverage")
    assert baseline.headers["Location"] == f"/runs/{demo.baseline_run}"


@pytest.mark.parametrize(
    "path",
    [
        "/repos/999",
        "/repos/1/prs/99",
        "/runs/run_nope",
        "/runs/bad%20id",
        "/runs/run_nope/events",
        "/repos/1/files/../../etc/passwd",
        "/repos/1/files/pyproject.toml",
        "/repos/1/files/src/ledger/nope.py",
        "/repos/1/functions/zzzz",
        "/repos/1/functions/0000000000000000",
        "/repos/1/map?run=run_nope",
    ],
)
async def test_unknown_things_are_404(client: Client, demo: Demo, path: str) -> None:
    assert (await client.get(path)).status_code == 404


async def test_outbox_renders_markdown_without_raw_html(client: Client) -> None:
    html = await (await client.get("/forge/outbox")).get_data(as_text=True)
    assert "Would be posted to GitHub (fake forge)" in html
    assert "<h3>coverset · PASS with gaps</h3>" in html or "coverset · PASS with gaps" in html
    assert "<table>" in html and "<details><summary>" in html
    assert "coverset:sticky" not in html


async def test_quarantine_toggle_round_trip(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/repos/{demo.repo_id}/tests")).get_data(as_text=True)
    token = re.search(r'"X-CSRF-Token": "([^"]+)"', page)
    assert token
    test_id = int(re.search(r'id="test-(\d+)"', page).group(1))  # type: ignore[union-attr]
    url = f"/repos/{demo.repo_id}/tests/{test_id}/quarantine"
    headers = {**HX, "X-CSRF-Token": token.group(1)}
    on = await client.post(url, headers=headers, form={"quarantined": "1"})
    assert on.status_code == 200
    assert 'aria-pressed="true"' in await on.get_data(as_text=True)
    off = await client.post(url, headers=headers, form={"quarantined": "0"})
    assert 'aria-pressed="false"' in await off.get_data(as_text=True)
    assert (await client.post(url, form={"quarantined": "1"})).status_code == 403  # no CSRF token
