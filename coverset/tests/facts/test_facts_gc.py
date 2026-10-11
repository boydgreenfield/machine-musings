"""Run retention: expire old runs, keep the protected ones, never touch the dictionary."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pytest
from facts_helpers import make_ctx

from coverset.facts.convert import read_python_bundle
from coverset.facts.dictionary import DICTIONARY_FILE
from coverset.facts.gc import expire_runs
from coverset.facts.ingest import ingest_shard
from coverset.facts.store import FactsStore

CUTOFF = datetime(2026, 10, 2, 18, tzinfo=UTC)


@pytest.fixture
def runs(store: FactsStore, minimal_bundle: Path) -> FactsStore:
    bundle = read_python_bundle(minimal_bundle)
    for day in (1, 2, 3):
        ingest_shard(store, make_ctx(f"run-{day}", day=day), bundle, 0)
    ingest_shard(store, make_ctx("other-1", repo_id=2, day=1), bundle, 0)
    return store


def test_dry_run_deletes_nothing(runs: FactsStore) -> None:
    expired = expire_runs(runs, keep_run_ids=set(), older_than=CUTOFF, dry_run=True)
    assert expired == ["other-1", "run-1", "run-2"]
    assert runs.list_runs(1) == ["run-1", "run-2", "run-3"]


def test_expire_respects_keep_list_and_repo_filter(runs: FactsStore) -> None:
    expired = expire_runs(runs, keep_run_ids={"run-1"}, older_than=CUTOFF, repo_id=1)
    assert expired == ["run-2"]
    assert runs.list_runs(1) == ["run-1", "run-3"]
    assert runs.list_runs(2) == ["other-1"]
    assert (runs.repo_dir(1) / DICTIONARY_FILE).is_file()


def test_expire_everything_old_keeps_the_dictionary(runs: FactsStore) -> None:
    ids_before = runs.dictionary(1)
    assert expire_runs(runs, keep_run_ids=set(), older_than=datetime(2027, 1, 1)) == [
        "other-1",
        "run-1",
        "run-2",
        "run-3",
    ]
    assert runs.list_runs(1) == [] and runs.list_runs(2) == []
    assert runs.dictionary(1).test_id("tests/test_calc.py::test_add") == ids_before.test_id(
        "tests/test_calc.py::test_add"
    )


def test_runs_without_runs_dim_use_directory_age(store: FactsStore) -> None:
    store.write_table(
        1, "crashed", "files", pa.table({"file_id": [0], "path": ["a.py"]}), shard_id=0, attempt=1
    )
    old = datetime(2026, 9, 1, tzinfo=UTC).timestamp()
    os.utime(store.run_dir(1, "crashed"), (old, old))
    assert expire_runs(store, keep_run_ids=set(), older_than=CUTOFF) == ["crashed"]
