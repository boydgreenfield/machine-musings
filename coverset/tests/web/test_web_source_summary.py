"""The source view's summary line, multi-line statements and syntax tokens on the demo, and the
status line above the maps."""

from __future__ import annotations

import html as html_lib
import re

from quart.typing import TestClientProtocol as Client
from web_helpers import Demo, assert_csp_clean

from coverset.facts.store import FactsStore
from coverset.web.facts_view import run_facts

PARSE = "src/ledger/durations/parse.py"


def _summary(page: str) -> str:
    found = re.search(r'<span class="src-summary" data-file-stats>(.*?)</span></span>', page, re.S)
    assert found, "the summary line"
    return html_lib.unescape(re.sub(r"<[^>]+>", "", found.group(1) + "</span>"))


async def test_the_summary_line_of_parse_py(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/repos/{demo.repo_id}/files/{PARSE}?run={demo.pr2_run}")).get_data(
        as_text=True
    )
    assert_csp_clean(page)
    store = FactsStore(demo.data_dir / "facts")
    row = next(
        r for r in store.read_table(demo.repo_id, demo.pr2_run, "file_cov").to_pylist() if r["path"] == PARSE
    )
    facts = run_facts(store, demo.repo_id, demo.pr2_run)
    tests = len(facts.tests_in_range(PARSE, 1, 10_000))
    statements, covered = row["statements"], row["covered"]
    text = _summary(page)
    assert text.startswith(f"lines {100.0 * covered / statements:.1f}% ({covered} of {statements}) · ")
    branches, taken = row["branches"], row["branches_covered"]
    assert branches and f" · branches {100.0 * taken / branches:.1f}% ({taken} of {branches}) · " in text
    found = re.search(r"input classes (\d+) of (\d+) \(([\d.]+)%\) · gap (\d+) pts · ", text)
    assert found, text
    observed, expected = int(found.group(1)), int(found.group(2))
    assert 0 < observed < expected and found.group(3) == f"{100.0 * observed / expected:.1f}"
    gap = max(0.0, 100.0 * covered / statements - 100.0 * observed / expected)
    assert int(found.group(4)) == round(gap)
    assert text.endswith(f" · {tests} test{'' if tests == 1 else 's'}")
    # one line: the pieces never break inside
    assert text.count(" · ") == 4


async def test_every_file_has_one_summary_line(client: Client, demo: Demo) -> None:
    facts = run_facts(FactsStore(demo.data_dir / "facts"), demo.repo_id, demo.pr2_run)
    kinds = set()
    for path in sorted(facts.statics):
        page = await (await client.get(f"/repos/{demo.repo_id}/files/{path}?run={demo.pr2_run}")).get_data(
            as_text=True
        )
        text = _summary(page)
        assert re.match(r"(lines [\d.]+% \(\d+ of \d+\)|no statements) · ", text), text
        if " · no input classes · " in text:
            kinds.add("none")
            assert "gap" not in text, text
        else:
            kinds.add("classes")
            assert re.search(r" · input classes \d+ of \d+ \([\d.]+%\) · gap \d+ pts · \d+ tests?$", text), (
                text
            )
    assert kinds == {"none", "classes"}


async def test_source_view_tokens_and_continuations(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/repos/{demo.repo_id}/files/{PARSE}?run={demo.pr2_run}")).get_data(
        as_text=True
    )
    assert 'class="tk-k">def</span>' in page and 'class="tk-nf">' in page
    # every continuation line points at a line that exists and is not itself a continuation
    conts = re.findall(
        r'<div class="ln[^"]* cont[^"]*" id="L(\d+)" data-line="\d+" data-first="(\d+)">', page
    )
    starts = {int(n) for n in re.findall(r'<div class="ln[^"]*" id="L(\d+)" data-line="\d+">', page)}
    assert all(int(first) in starts and int(first) < int(n) for n, first in conts)


async def test_the_maps_have_a_status_line(client: Client, demo: Demo) -> None:
    for url in (f"/repos/{demo.repo_id}/map", f"/repos/{demo.repo_id}"):
        page = await (await client.get(url)).get_data(as_text=True)
        assert_csp_clean(page)
        box = page.split("data-tm-box>", 1)
        assert len(box) == 2, url
        assert (
            box[1]
            .lstrip()
            .startswith(
                '<p class="tm-status" data-tm-status aria-hidden="true"><span class="tm-status-idle">hover a tile</span></p>'
            )
        ), url
        infos = re.findall(r'<a [^>]*class="tm-leaf-link"[^>]*data-info="([^"]+)"', box[1])
        assert infos and all(" statements · lines " in html_lib.unescape(i) for i in infos), url
    page = await (await client.get(f"/repos/{demo.repo_id}/map")).get_data(as_text=True)
    assert "area = statements; click a function to open its source" in page
