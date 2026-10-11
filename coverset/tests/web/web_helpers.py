"""Helpers shared by the web UI tests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

HX = {"HX-Request": "true"}
"""Headers of an htmx (non-boosted) request: the route answers with a fragment."""
BOOSTED = {"HX-Request": "true", "HX-Boosted": "true"}

INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
EVENT_HANDLER = re.compile(r"<[^>]+\son[a-z]+\s*=", re.IGNORECASE)


def assert_csp_clean(html: str) -> None:
    """No inline scripts and no inline event handlers (the CSP would block them)."""
    assert not INLINE_SCRIPT.search(html), INLINE_SCRIPT.search(html)
    assert not EVENT_HANDLER.search(html), EVENT_HANDLER.search(html)
    assert "hx-on" not in html
    assert "javascript:" not in html.lower()


@dataclass(frozen=True)
class Demo:
    """Where the demo data lives and the ids the tests need."""

    data_dir: Path
    db_url: str
    pr1_run: str
    pr2_run: str
    baseline_run: str
    repo_id: int
