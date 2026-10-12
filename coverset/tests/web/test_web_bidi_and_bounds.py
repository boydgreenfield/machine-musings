"""Bidirectional controls are shown as markers on every page, and out-of-range ids are 404s."""

from __future__ import annotations

from quart.typing import TestClientProtocol as Client
from web_helpers import Demo

from coverset.web.pages import nodeid_filter
from coverset.web.render.html import breakable, mark_bidi


def test_nodeid_filter_marks_bidirectional_controls() -> None:
    rendered = str(nodeid_filter("tests/test_x.py::test_a‮b[⁦c]"))
    assert "‮" not in rendered and "⁦" not in rendered
    assert "&lt;U+202E&gt;" in rendered and "&lt;U+2066&gt;" in rendered
    assert mark_bidi("plain") == "plain" and mark_bidi(None) == ""
    assert "&lt;U+202E&gt;" in str(breakable("a‮b"))


async def test_out_of_range_ids_are_404(client: Client, demo: Demo) -> None:
    huge = 2**63
    assert (await client.get(f"/repos/{huge}")).status_code == 404
    assert (await client.get(f"/repos/{demo.repo_id}/prs/{huge}")).status_code == 404
    assert (await client.get(f"/repos/{huge}/prs/1")).status_code == 404
