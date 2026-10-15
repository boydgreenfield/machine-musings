"""The Rust fixture through the orchestrator's ingest, as an uploaded shard would go.

``fixtures/bundles/neutral-rust/bundle.tar.gz`` is ingested as shard 0 of a run with
``language="rust"`` (:func:`~coverset.orchestrator.bundles.ingest_one`: safe extraction, the reader
picked by the manifest, the :class:`~coverset.lang.rust.RustAdapter` for the language), then
finalized; the answers come from the ordinary facts queries. Paths are the crate's own here (the
crate is the repository).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from coverset.facts import queries
from coverset.facts.ingest import RunContext, finalize_run
from coverset.facts.untrusted import BundleLimits
from coverset.lang import adapter_for_language, read_any_bundle
from coverset.orchestrator.bundles import IngestJob, ingest_one
from coverset.storage.testing import local_blob_url, local_facts_store

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "bundles" / "neutral-rust"
MONEY, DURATIONS = "src/money.rs", "src/durations.rs"
HEAD = frozenset(
    {
        "Cargo.toml",
        "src/lib.rs",
        MONEY,
        DURATIONS,
        "src/emailcheck.rs",
        "src/error.rs",
        "src/intervals.rs",
        "tests/ledger_flow.rs",
    }
)
REPO, RUN = 11, "rust-run-1"


@pytest.fixture(scope="module")
def ingested(tmp_path_factory: pytest.TempPathFactory) -> dict:  # type: ignore[type-arg]
    facts = tmp_path_factory.mktemp("facts")
    ctx = RunContext(
        REPO, RUN, "a" * 40, "b" * 40, "feature/rust", 3, datetime(2026, 10, 13, 12, tzinfo=UTC), None
    )
    job = IngestJob(
        facts_dir=str(facts),
        blob_url=local_blob_url(facts),
        ctx=ctx,
        shard_id=0,
        tarball=str(FIXTURE / "bundle.tar.gz"),
        limits=BundleLimits(),
        source_root=str(FIXTURE / "repo"),
        head_paths=HEAD,
        functions=(),
        keep_paths=frozenset({MONEY}),
        language="rust",
    )
    outcome = ingest_one(job)
    assert outcome.ok, outcome.error
    bundle = read_any_bundle(FIXTURE / "bundle.tar.gz", head_paths=HEAD)
    store = local_facts_store(facts)
    adapter = adapter_for_language("rust")
    finalized = finalize_run(store, ctx, expected_shards=1, catalogue=adapter.shape_catalogue())
    return {"store": store, "bundle": bundle, "outcome": outcome, "finalized": finalized, "job": job}


def test_the_shard_is_ingested_with_shipped_regions(ingested: dict) -> None:  # type: ignore[type-arg]
    outcome, store = ingested["outcome"], ingested["store"]
    assert len(outcome.collected) == 16 and len(outcome.tests) == 16
    assert ingested["finalized"].coverage_complete and ingested["finalized"].functions == 12
    functions = store.read_table(REPO, RUN, "functions").to_pylist()
    assert {(f["qualname"], f["language"]) for f in functions} >= {
        ("Ledger::post", "rust"),
        ("allocate_cents", "rust"),
    }
    summary = queries.run_summary(store, REPO, RUN)
    assert summary is not None and summary.outcomes == {"passed": 15, "skipped": 1}
    assert summary.partial_branches == 0  # no branch data for Rust in v1


def test_tests_covering_a_function_and_patch_coverage(ingested: dict) -> None:  # type: ignore[type-arg]
    store = ingested["store"]
    transfer = next(r for r in ingested["bundle"].functions if r.qualname == "Ledger::transfer")
    assert queries.tests_covering(store, REPO, RUN, MONEY, transfer.body_start, transfer.end_line) == [
        "ledger::ledger_flow a_failed_transfer_reports_the_shortfall"
    ]
    assert queries.line_tests(store, REPO, RUN, MONEY, 22) == ["ledger money::tests::refuses_zero_weights"]
    # a diff over transfer's body: line 85 (the second posting) never ran
    patch = queries.patch_coverage(store, REPO, RUN, {MONEY: {84, 85, 86}})
    assert (patch.covered, patch.total) == (1, 3) and patch.missing == {MONEY: [85, 86]}
    # a diff inside the test module touches no measurable line
    assert queries.patch_coverage(store, REPO, RUN, {MONEY: {116, 117}}).total == 0


def test_the_upload_path_keeps_the_rust_language() -> None:
    """``rust`` is a registered neutral language, so upload mode does not mistake a Rust manifest
    for a Python bundle (``orchestrator/upload.py`` maps unregistered languages to ``python``)."""
    from coverset.orchestrator import upload

    neutral_language = getattr(upload, "_neutral_language", None)
    if neutral_language is None:
        pytest.skip("upload mode no longer has _neutral_language")
    assert neutral_language(FIXTURE / "bundle.tar.gz") == "rust"
