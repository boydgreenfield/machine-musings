"""Helpers shared by the web UI tests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coverset.config import Settings
from coverset.db.testing import run_sql
from coverset.facts.store import FactsStore, facts_store

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
    """Where the demo data lives and the ids the tests need.

    The demo follows the suite's backends (``tests/web/conftest.py``): ``db_url`` is a database cloned
    from the worker's template on the test server; ``blob_url`` is empty (facts under
    ``data_dir``) or, with ``COVERSET_TEST_BLOB_URL``, a key prefix of that store, with ``data_dir``
    holding only the cache. Tests reach the data through :meth:`overrides`, :meth:`settings`,
    :meth:`facts` and :meth:`rows`, never through a backend's own files.
    """

    data_dir: Path
    db_url: str
    pr1_run: str
    pr2_run: str
    baseline_run: str
    repo_id: int
    blob_url: str = ""

    def overrides(self) -> dict[str, Any]:
        """The :class:`Settings` fields that point an app at the demo."""
        values: dict[str, Any] = {"data_dir": self.data_dir, "database_url": self.db_url}
        if self.blob_url:
            values["blob_url"] = self.blob_url
        return values

    def settings(self) -> Settings:
        """Test settings for the demo's state."""
        return Settings.for_tests(**self.overrides())

    def facts(self) -> FactsStore:
        """The demo's facts store (its local files, or the cache in front of its bucket prefix)."""
        return facts_store(self.settings())

    def rows(self, statement: str, params: Mapping[str, Any] | None = None) -> list[tuple[Any, ...]]:
        """Run one SQL statement (``:name`` parameters) on the demo's database, committing it, and
        return its rows, on either backend (:func:`coverset.db.testing.run_sql`)."""
        return run_sql(self.db_url, statement, params)
