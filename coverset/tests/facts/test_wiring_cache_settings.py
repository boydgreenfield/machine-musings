"""The facts layer's cache knobs: the app's configured values win over the environment, and an
ingest job carries them into its worker process."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from facts_helpers import make_ctx

from coverset.facts import cache
from coverset.facts.convert import read_python_bundle
from coverset.facts.ingest import ingest_shard
from coverset.facts.store import FactsStore
from coverset.facts.untrusted import BundleLimits
from coverset.lang.base import FunctionRegion
from coverset.orchestrator.bundles import IngestJob, ingest_one


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "COVERSET_FACTS_THREADS",
        "COVERSET_FACTS_MEMORY_LIMIT",
        "COVERSET_FACTS_CACHE_ENTRIES",
        "COVERSET_FACTS_RESULT_ENTRIES",
        cache.COMPACT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    cache.configure(None)


def test_configured_values_win_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COVERSET_FACTS_THREADS", "3")
    monkeypatch.setenv("COVERSET_FACTS_MEMORY_LIMIT", "300MB")
    assert cache.threads() == 3 and cache.memory_limit() == "300MB"
    cache.configure(
        {
            "COVERSET_FACTS_THREADS": "2",
            "COVERSET_FACTS_MEMORY_LIMIT": "1GB",
            "COVERSET_FACTS_CACHE_ENTRIES": 5,
            "COVERSET_FACTS_RESULT_ENTRIES": 7,
            "COVERSET_FACTS_COMPACT": None,  # None leaves the environment in charge
        }
    )
    assert cache.threads() == 2 and cache.memory_limit() == "1GB"
    assert cache.ConnectionCache().max_entries == 5 and cache.ResultCache().max_entries == 7
    assert cache.ConnectionCache(max_entries=1).max_entries == 1, "a constructor bound still wins"
    assert cache.override("COVERSET_FACTS_COMPACT") is None and not cache.compact_layout()
    monkeypatch.setenv(cache.COMPACT_ENV, "1")
    assert cache.compact_layout()
    cache.configure({cache.COMPACT_ENV: "0"})
    assert not cache.compact_layout(), "the configured value wins over the environment"
    cache.configure(None)
    assert cache.threads() == 3 and cache.overrides() == {}


def test_a_bad_configured_memory_limit_falls_back_to_the_default() -> None:
    cache.configure({"COVERSET_FACTS_MEMORY_LIMIT": "1GB'; SET threads=64; --"})
    assert cache.memory_limit() == "512MB"


def test_the_configured_compact_flag_reaches_the_ingest(
    store: FactsStore, minimal_bundle: Path, functions: list[FunctionRegion]
) -> None:
    bundle = read_python_bundle(minimal_bundle)
    cache.configure({cache.COMPACT_ENV: "1"})
    ingest_shard(store, make_ctx("run-1"), bundle, 0, functions)
    assert store.table_files(1, "run-1", "test_file_bits")


def test_an_ingest_job_applies_its_overrides_in_the_worker(
    store: FactsStore, minimal_bundle: Path, functions: list[FunctionRegion], tmp_path: Path
) -> None:
    """A process-pool worker shares the parent's environment, not its configured registry: the job
    carries ``Settings.cache_overrides()`` and :func:`ingest_one` applies them first."""
    tarball = tmp_path / "bundle.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        for member in sorted(minimal_bundle.iterdir()):
            tar.add(member, arcname=member.name)
    job = IngestJob(
        facts_dir=str(store.root),
        ctx=make_ctx("run-2"),
        shard_id=0,
        tarball=str(tarball),
        limits=BundleLimits(),
        source_root=str(tmp_path),
        head_paths=None,
        functions=tuple(functions),
        cache_overrides=((cache.COMPACT_ENV, "1"),),
    )
    outcome = ingest_one(job)
    assert outcome.ok, outcome.error
    assert store.table_files(1, "run-2", "test_file_bits")
    assert cache.override(cache.COMPACT_ENV) == "1"
