"""``?test=<id>`` on the source view: the data-flow lens of one test (its lines stay, the rest dim)."""

from __future__ import annotations

import html as html_lib
import re

from quart.typing import TestClientProtocol as Client
from web_helpers import Demo, assert_csp_clean

REFUND = "src/ledger/billing/refund.py"
FAILING = "tests/test_refund.py::test_refund_partial"


async def _test_id(client: Client, demo: Demo) -> int:
    response = await client.get(f"/repos/{demo.repo_id}/tests/by-nodeid?nodeid={FAILING}&run={demo.pr1_run}")
    assert response.status_code == 302
    found = re.fullmatch(
        rf"/repos/{demo.repo_id}/tests/(\d+)\?run={demo.pr1_run}", response.headers["Location"]
    )
    assert found
    return int(found.group(1))


async def test_the_source_view_dims_lines_the_test_did_not_run(client: Client, demo: Demo) -> None:
    test_id = await _test_id(client, demo)
    url = f"/repos/{demo.repo_id}/files/{REFUND}?run={demo.pr1_run}&test={test_id}"
    page = await (await client.get(url)).get_data(as_text=True)
    assert_csp_clean(page)
    assert 'id="focus-note"' in page and 'class="src-focus"' in page
    note = html_lib.unescape(re.search(r'id="focus-note">(.*?)</span>', page, re.S).group(1))
    note = note.replace("<wbr>", "")
    assert "test_refund_partial" in note and "the rest dimmed" in note
    assert re.search(r'<div class="ln run[^"]*"', page), "the test's own lines keep their state"
    kept = re.findall(r'<div class="ln (?:run|par)(?! [^"]*dim)[^"]*" id="L(\d+)"', page)
    dimmed = re.findall(r'<div class="ln [^"]*dim[^"]*" id="L(\d+)"', page)
    assert kept and dimmed and not set(kept) & set(dimmed)
    assert f'href="/repos/{demo.repo_id}/tests/{test_id}?run={demo.pr1_run}"' in page
    clear = re.search(r'<a href="([^"]+)">show every test</a>', page)
    assert clear and "test=" not in clear.group(1)


async def test_a_test_that_never_touched_the_file_dims_everything(client: Client, demo: Demo) -> None:
    test_id = await _test_id(client, demo)
    url = f"/repos/{demo.repo_id}/files/src/ledger/durations/parse.py?run={demo.pr1_run}&test={test_id}"
    page = await (await client.get(url)).get_data(as_text=True)
    assert "0 of" in html_lib.unescape(re.search(r'id="focus-note">(.*?)</span>', page, re.S).group(1))
    assert not re.findall(r'<div class="ln (?:run|par)(?! [^"]*dim)[^"]*" id="L', page)


async def test_without_the_parameter_nothing_is_dimmed_and_bad_ids_are_404(
    client: Client, demo: Demo
) -> None:
    plain = await (await client.get(f"/repos/{demo.repo_id}/files/{REFUND}?run={demo.pr1_run}")).get_data(
        as_text=True
    )
    assert 'id="focus-note"' not in plain and "dim" not in re.findall(r'<div class="ln [^"]*"', plain)[0]
    missing = await client.get(f"/repos/{demo.repo_id}/files/{REFUND}?run={demo.pr1_run}&test=99999999")
    assert missing.status_code == 404
    junk = await client.get(f"/repos/{demo.repo_id}/files/{REFUND}?run={demo.pr1_run}&test=abc")
    assert junk.status_code in (200, 400)
