"""The PR page's diff analysis cache is keyed by the run's stamp, never by the facts object's id."""

from __future__ import annotations

import gc
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from coverset.facts.schema import SCHEMAS
from coverset.facts.store import FactsStore
from coverset.web import pr_view
from coverset.web.facts_view import RunFacts
from coverset.web.pr_view import DiffAnalysis, analyse_diff, cache_key

HEAD = "a" * 40


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Every real analysis (``(run_id, id(facts))``); the module cache starts empty."""
    seen: list[tuple[str, int]] = []

    def analyse(mirror: Path, base: str | None, head: str, facts: RunFacts) -> DiffAnalysis:
        seen.append((facts.run_id, id(facts)))
        return DiffAnalysis(base=base, head=head)

    monkeypatch.setattr(pr_view, "_analyse", analyse)
    monkeypatch.setattr(pr_view, "_cache", type(pr_view._cache)())
    return seen


def _write_table(store: FactsStore, run_id: str, table: str = "file_cov") -> None:
    """One more run-level table in the run's directory (what an ingest or a finalize does)."""
    store.write_table(1, run_id, table, pa.Table.from_pylist([], schema=SCHEMAS[table]))


def test_a_new_facts_object_for_an_unchanged_run_hits_the_cache(tmp_path: Path, calls: list[Any]) -> None:
    store = FactsStore(tmp_path / "facts")
    _write_table(store, "run-1")
    first = analyse_diff(tmp_path, "b" * 40, HEAD, RunFacts(store, 1, "run-1"))
    again = analyse_diff(tmp_path, "b" * 40, HEAD, RunFacts(store, 1, "run-1"))
    assert first is again and len(calls) == 1


def test_a_reingested_run_is_analysed_again_even_if_the_facts_id_is_reused(
    tmp_path: Path, calls: list[Any]
) -> None:
    """The stale case the id key allowed: the cached object is dropped, a new one for the re-ingested
    run may get the same ``id``; the stamp differs, so the analysis is recomputed."""
    store = FactsStore(tmp_path / "facts")
    _write_table(store, "run-1")
    facts = RunFacts(store, 1, "run-1")
    before = cache_key(tmp_path, None, HEAD, facts)
    analyse_diff(tmp_path, None, HEAD, facts)
    del facts
    gc.collect()
    _write_table(store, "run-1", "run_economy")  # a re-ingest or finalize writes more
    fresh = RunFacts(store, 1, "run-1")
    assert cache_key(tmp_path, None, HEAD, fresh) != before
    analyse_diff(tmp_path, None, HEAD, fresh)
    assert len(calls) == 2


def test_runs_and_stores_never_share_an_entry(tmp_path: Path, calls: list[Any]) -> None:
    one, two = FactsStore(tmp_path / "one"), FactsStore(tmp_path / "two")
    for store in (one, two):
        _write_table(store, "run-1")
    _write_table(one, "run-2")
    for facts in (RunFacts(one, 1, "run-1"), RunFacts(one, 1, "run-2"), RunFacts(two, 1, "run-1")):
        analyse_diff(tmp_path, None, HEAD, facts)
    assert [run for run, _ in calls] == ["run-1", "run-2", "run-1"]


def test_facts_without_a_store_are_not_cached(tmp_path: Path, calls: list[Any]) -> None:
    class Storeless(RunFacts):
        def __init__(self) -> None:
            self.store = None  # type: ignore[assignment]
            self.repo_id, self.run_id = 0, "bundle"

    assert cache_key(tmp_path, None, HEAD, Storeless()) is None
    analyse_diff(tmp_path, None, HEAD, Storeless())
    analyse_diff(tmp_path, None, HEAD, Storeless())
    assert len(calls) == 2
