"""End to end: a TypeScript neutral bundle through the orchestrator's ingest into facts and answers.

The synthetic fixture (``fixtures/bundles/neutral-ts``, written by ``scripts/make_neutral_fixture.py``)
is ingested as shard 0 of a run of an invented repository, exactly as a worker's tarball is
(:func:`~coverset.orchestrator.bundles.ingest_one`: safe extraction, the reader picked by the
manifest, function regions from ``functions.jsonl`` because nothing indexed the TypeScript sources),
then finalized with the neutral adapter's catalogue. Every answer below comes from the existing facts
queries and the input-class catalogue; the Python reader and the Python adapter are made to fail
if anything calls them.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coverset.facts import queries
from coverset.facts.ingest import RunContext, finalize_run
from coverset.facts.untrusted import BundleLimits
from coverset.lang import read_any_bundle
from coverset.lang.neutral import NeutralAdapter
from coverset.orchestrator.bundles import IngestJob, ingest_one, job_regions
from coverset.storage.testing import local_blob_url, local_facts_store
from coverset_model.catalogue import NEVER_EXERCISED, function_catalogue_from_types

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "bundles" / "neutral-ts"
PRICING, STRINGS, RATES = "src/cart/pricing.ts", "src/util/strings.ts", "src/shipping/rates.ts"
P, S, R = "tests/pricing.test.ts", "tests/strings.test.ts", "tests/rates.test.ts"
HEAD = frozenset({PRICING, STRINGS, RATES, P, S, R})
REPO, RUN = 7, "ts-run-1"


def ingest_ts_fixture(facts: Path) -> dict:  # type: ignore[type-arg]
    """Ingest the TypeScript fixture as shard 0 of ``RUN`` into the facts store at ``facts``.

    Shared with ``tests/web/test_web_neutral.py``. Returns the store, the bundle, the ingest
    outcome and the finalize result; the Python reader and adapter fail if anything calls them.
    """
    mp = pytest.MonkeyPatch()

    def no_python(*args: object, **kwargs: object) -> None:
        raise AssertionError("a Python-specific code path ran for a neutral bundle")

    mp.setattr("coverset.facts.convert.read_python_bundle", no_python)
    mp.setattr("coverset.lang.python.PythonAdapter.read_bundle", no_python)
    mp.setattr("coverset.lang.python.PythonAdapter.index_functions", no_python)
    ctx = RunContext(
        REPO, RUN, "d" * 40, "e" * 40, "feature/ts", 12, datetime(2026, 10, 11, 12, tzinfo=UTC), None
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
        keep_paths=frozenset({PRICING}),
        language="typescript",
    )
    try:
        outcome = ingest_one(job)
        assert outcome.ok, outcome.error
        bundle = read_any_bundle(FIXTURE / "bundle.tar.gz", head_paths=HEAD)
        store = local_facts_store(facts)
        result = finalize_run(
            store, ctx, expected_shards=1, catalogue=NeutralAdapter.from_bundle(bundle).shape_catalogue()
        )
    finally:
        mp.undo()
    return {"store": store, "bundle": bundle, "outcome": outcome, "finalized": result}


@pytest.fixture(scope="module")
def ingested(tmp_path_factory: pytest.TempPathFactory) -> dict:  # type: ignore[type-arg]
    return ingest_ts_fixture(tmp_path_factory.mktemp("facts"))


def _region(ingested: dict, qualname: str):  # type: ignore[type-arg]  # noqa: ANN202
    return next(r for r in ingested["bundle"].functions if r.qualname == qualname)


def test_the_shard_is_ingested_with_shipped_regions(ingested: dict) -> None:  # type: ignore[type-arg]
    outcome, store = ingested["outcome"], ingested["store"]
    assert len(outcome.tests) == 11 and len(outcome.collected) == 12
    assert outcome.union_lines[PRICING] == frozenset({1, 2, 3, 6, 7, 10, 11, 14, 15, 16, 19, 20, 23, 24, 26})
    assert ingested["finalized"].coverage_complete and ingested["finalized"].functions == 8
    functions = store.read_table(REPO, RUN, "functions").to_pylist()
    assert {(f["qualname"], f["language"]) for f in functions} >= {
        ("rateFor", "typescript"),
        ("Cart.addItem", "typescript"),
    }
    rate = next(f for f in functions if f["qualname"] == "rateFor")
    assert json.loads(rate["types_json"])["zone"] == {
        "kind": "literal",
        "values": ["domestic", "eu", "world"],
    }
    summary = queries.run_summary(store, REPO, RUN)
    assert summary is not None
    assert summary.outcomes == {"passed": 8, "failed": 2, "skipped": 1}


def test_patch_coverage(ingested: dict) -> None:  # type: ignore[type-arg]
    store = ingested["store"]
    # A diff touching applyDiscount's body (7-11) and pad's (17), plus a blank and a comment-free line.
    patch = queries.patch_coverage(store, REPO, RUN, {PRICING: {7, 8, 9, 10, 11}, STRINGS: {17, 18}})
    assert (patch.covered, patch.total) == (4, 5)
    assert patch.missing == {PRICING: [8]} and patch.per_file == {PRICING: (3, 4), STRINGS: (1, 1)}
    assert patch.complete and patch.percent == 80.0
    assert queries.patch_coverage(store, REPO, RUN, {RATES: {12}}).missing == {RATES: [12]}


def test_tests_covering_a_function_and_a_line(ingested: dict) -> None:  # type: ignore[type-arg]
    store = ingested["store"]
    apply = _region(ingested, "applyDiscount")
    assert queries.tests_covering(store, REPO, RUN, PRICING, apply.body_start, apply.end_line) == [
        f"{P} > applyDiscount > rounds half cents",
        f"{P} > applyDiscount > takes 10% off",
    ]
    assert queries.line_tests(store, REPO, RUN, PRICING, 24) == [f"{P} > Cart > rejects a zero quantity"]
    assert queries.line_tests(store, REPO, RUN, PRICING, 6) == ["<import>"]
    free = _region(ingested, "isFreeShipping")
    assert queries.tests_covering(store, REPO, RUN, RATES, free.body_start, free.end_line) == []
    prior = queries.ParquetPriorRun(store, REPO, RUN)
    assert prior.tests_touching(STRINGS, 9, 13) == {
        f"{S} > parseQuantity > reads digits",
        f"{S} > parseQuantity > returns undefined for blank input",
    }
    assert {r.qualname for r in prior.functions()} == {r.qualname for r in ingested["bundle"].functions}


def test_the_partial_branch(ingested: dict) -> None:  # type: ignore[type-arg]
    summary = queries.run_summary(ingested["store"], REPO, RUN)
    assert summary is not None
    assert summary.partial_branches == 1  # pricing.ts line 7: no test passes a zero discount
    assert (summary.branches, summary.branches_covered) == (6, 5)


def test_input_classes_and_the_never_exercised_one(ingested: dict) -> None:  # type: ignore[type-arg]
    store, bundle = ingested["store"], ingested["bundle"]
    apply = _region(ingested, "applyDiscount")
    stored = queries.function_input_cov(store, REPO, RUN, apply.func_key)
    assert stored is not None and stored.tests_truncated == 0
    assert "totalCents:negative" in stored.missing["totalCents"]
    observations = [o for o in bundle.shapes if o.func == apply.triple]
    sampling = next(s for s in bundle.sampling if s.func == apply.triple)
    found = function_catalogue_from_types(apply, bundle.types[apply.triple])
    assert found.annotations == {"totalCents": "int", "percent": "float"} and found.returns == "int"
    cov = found.input_cov(apply, observations, sampling)
    assert "totalCents:negative" in {c.name for c in cov.missing}
    assert cov.missing_reason == NEVER_EXERCISED
    assert cov.observed_expected == stored.blocks_observed and len(cov.expected) == stored.blocks_expected
    rows = queries.shapes_for_function(store, REPO, apply.func_key, branch="feature/ts")
    assert {(r.param, r.shape_key) for r in rows} >= {("totalCents", "int|pos"), ("percent", "float|pos")}

    fmt = _region(ingested, "formatPrice")
    enum_cov = function_catalogue_from_types(fmt, bundle.types[fmt.triple]).input_cov(
        fmt, [o for o in bundle.shapes if o.func == fmt.triple], None
    )
    assert "currency:Currency.EUR" in enum_cov.observed
    assert "currency:Currency.USD" in {c.name for c in enum_cov.missing}


def test_the_sample_cap_count(ingested: dict) -> None:  # type: ignore[type-arg]
    store, bundle = ingested["store"], ingested["bundle"]
    rate = _region(ingested, "rateFor")
    stored = queries.function_input_cov(store, REPO, RUN, rate.func_key)
    assert stored is not None and stored.tests_truncated == 1 and stored.tests_called == 1
    sampling = next(s for s in bundle.sampling if s.func == rate.triple)
    cov = function_catalogue_from_types(rate, bundle.types[rate.triple]).input_cov(
        rate,
        [o for o in bundle.shapes if o.func == rate.triple],
        sampling,
        tests_called=(f"{R} > rateFor > charges extra weight above a kilogram",),
    )
    assert cov.tests_truncated == 1
    assert cov.missing_reason == "not observed in 2048 sampled calls (1 test hit the sample cap)"
    assert {"zone:'domestic'", "zone:'world'", "weightsGrams:zero-element"} <= {c.name for c in cov.missing}
    assert "zone:'eu'" in cov.observed


def _job(facts: Path, **overrides: object) -> IngestJob:
    ctx = RunContext(
        REPO, "ts-run-2", "d" * 40, "e" * 40, "feature/ts", 12, datetime(2026, 10, 11, 12, tzinfo=UTC), None
    )
    fields: dict = {  # type: ignore[type-arg]
        "facts_dir": str(facts),
        "blob_url": local_blob_url(facts),
        "ctx": ctx,
        "shard_id": 0,
        "tarball": str(FIXTURE / "bundle.tar.gz"),
        "limits": BundleLimits(),
        "source_root": str(FIXTURE / "repo"),
        "head_paths": HEAD,
        "functions": (),
        **overrides,
    }
    return IngestJob(**fields)


@pytest.mark.security
def test_a_python_job_takes_nothing_language_specific_from_the_bundle(tmp_path: Path) -> None:
    # The manifest says typescript and functions.jsonl ships regions and types; the job is Python.
    outcome = ingest_one(_job(tmp_path, language="python"))
    assert outcome.ok, outcome.error
    store = local_facts_store(tmp_path)
    assert store.read_table(REPO, "ts-run-2", "functions").num_rows == 0
    bundle = read_any_bundle(FIXTURE / "bundle.tar.gz", head_paths=HEAD)
    rate = _region({"bundle": bundle}, "rateFor")
    kept, types = job_regions(_job(tmp_path, language="python", functions=(rate,)), bundle)
    assert kept == [rate] and types == {}
    kept, types = job_regions(_job(tmp_path, language="typescript", functions=(rate,)), bundle)
    assert [r for r in kept if r.path == RATES] == [rate] and rate.triple not in types  # the index wins
    assert set(types) == {r.triple for r in kept if r.path != RATES} & set(bundle.types) != set()


def test_a_slow_read_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(job: IngestJob, bundle_dir: Path) -> None:
        time.sleep(2)

    monkeypatch.setattr("coverset.orchestrator.bundles.read_job_bundle", slow)
    outcome = ingest_one(_job(tmp_path, language="typescript", read_timeout_s=0.2))
    assert not outcome.ok and "longer than 0.2 s" in (outcome.error or "")
