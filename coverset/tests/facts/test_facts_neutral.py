"""Neutral bundles in the facts layer: the reader dispatch, the neutral adapter, a one-shard ingest."""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest
from facts_helpers import FIXTURE_DIR, make_ctx

from coverset.facts.ingest import finalize_run, ingest_shard
from coverset.facts.queries import function_input_cov, run_summary
from coverset.facts.untrusted import BundleError, BundleLimits
from coverset.lang import adapter_for_language, bundle_kind, read_any_bundle
from coverset.lang.base import FuncSampling, FunctionRegion, LanguageAdapter
from coverset.lang.neutral import (
    NEUTRAL_LANGUAGES,
    NeutralAdapter,
    NotRunnableError,
    merge_regions,
    register_language,
)
from coverset.lang.python import PythonAdapter
from coverset.orchestrator.bundles import merge_sampling
from coverset.storage.testing import local_facts_store
from coverset_model.io import write_bundle

NEUTRAL_TS = FIXTURE_DIR.parent / "neutral-ts"
PRICING = "src/cart/pricing.ts"


def test_read_any_bundle_picks_the_reader_by_manifest(tmp_path: Path) -> None:
    neutral = read_any_bundle(NEUTRAL_TS / "bundle")
    assert neutral.meta["bundle"]["language"] == "typescript" and len(neutral.functions) == 8
    archived = read_any_bundle(NEUTRAL_TS / "bundle.tar.gz")
    assert archived.tests == neutral.tests and archived.types == neutral.types
    python = read_any_bundle(FIXTURE_DIR / "bundle")
    assert python.functions == () and "convert" in python.meta and "bundle" not in python.meta
    assert read_any_bundle(FIXTURE_DIR / "bundle.tar.gz").tests == python.tests
    assert bundle_kind(NEUTRAL_TS / "bundle") == "neutral" and bundle_kind(FIXTURE_DIR / "bundle") == "python"
    # A Python bundle written in the neutral format is read by the neutral reader.
    write_bundle(python, tmp_path / "py-neutral")
    again = read_any_bundle(tmp_path / "py-neutral")
    assert again.meta["bundle"]["language"] == "python"
    assert sorted(again.tests, key=lambda t: t.nodeid) == sorted(python.tests, key=lambda t: t.nodeid)


def test_read_any_bundle_applies_limits_and_the_head_tree(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="neutral bundle refused"):
        read_any_bundle(NEUTRAL_TS / "bundle", limits=BundleLimits(max_tests=3))
    with pytest.raises(BundleError):
        read_any_bundle(NEUTRAL_TS / "bundle.tar.gz", limits=BundleLimits(max_bundle_bytes=100))
    head = {PRICING, "tests/pricing.test.ts"}
    bundle = read_any_bundle(NEUTRAL_TS / "bundle", head_paths=head)
    assert set(bundle.static) == {PRICING}
    assert {r.path for r in bundle.functions} == {PRICING}
    assert bundle.meta["convert"]["dropped"]["files.paths_not_in_head"] == 2
    with pytest.raises(BundleError):
        read_any_bundle(tmp_path / "missing")
    limits = BundleLimits(max_tests=7, max_line_number=99)
    assert (limits.max_tests, limits.max_line_number, limits.max_functions) == (7, 99, 200_000)


def test_the_neutral_adapter() -> None:
    bundle = read_any_bundle(NEUTRAL_TS / "bundle")
    adapter = NeutralAdapter.from_bundle(bundle)
    assert isinstance(adapter, LanguageAdapter) and adapter.name == "typescript"
    assert [r.qualname for r in adapter.index_functions(PRICING, "ignored")] == [
        "applyDiscount", "formatPrice", "Cart.addItem",
    ]  # fmt: skip
    assert adapter.detect(NEUTRAL_TS / "bundle") and not adapter.detect(FIXTURE_DIR / "bundle")
    with pytest.raises(NotRunnableError, match="not runnable by coverset"):
        adapter.env_spec(NEUTRAL_TS / "repo", {})
    with pytest.raises(NotRunnableError):
        adapter.test_command("args", None, "out", {})
    assert adapter.read_bundle(NEUTRAL_TS / "bundle").tests == bundle.tests
    catalogue = adapter.shape_catalogue({"ignored": "source"})
    apply = next(r for r in bundle.functions if r.qualname == "applyDiscount")
    assert catalogue.annotations_of(apply.triple)["totalCents"].kind == "int"
    assert isinstance(adapter_for_language("typescript"), NeutralAdapter)
    assert isinstance(adapter_for_language("python"), PythonAdapter)
    with pytest.raises(BundleError, match="no language adapter"):
        adapter_for_language("cobol")
    register_language("cobol")
    try:
        assert adapter_for_language("cobol").name == "cobol"
    finally:
        NEUTRAL_LANGUAGES.discard("cobol")
    with pytest.raises(ValueError):
        register_language("Not A Name")


def test_changed_functions_from_shipped_regions() -> None:
    head = read_any_bundle(NEUTRAL_TS / "bundle")
    apply, fmt, add_item = (r for r in head.functions if r.path == PRICING)
    alone = NeutralAdapter.from_bundle(head)
    assert [(c.region.qualname, c.kind) for c in alone.changed_functions(PRICING, "a", "b", {10}, set())] == [
        ("applyDiscount", "modified")
    ]
    base = [
        dataclasses.replace(apply, body_hash="older"),
        fmt,
        FunctionRegion("k", PRICING, "removed", 0, 30, 31, 32, "x", "y", (), "function"),
    ]
    changes = alone.with_base(base).changed_functions(PRICING, "old", "new", {15, 23}, set())
    assert [(c.kind, (c.region or c.prev).qualname) for c in changes] == [
        ("modified", "applyDiscount"), ("trivial", "formatPrice"), ("added", "Cart.addItem"), ("deleted", "removed"),
    ]  # fmt: skip
    deleted = alone.with_base(base).changed_functions(PRICING, "old", None, set(), {1})
    assert {c.kind for c in deleted} == {"deleted"}
    assert merge_regions([apply], [fmt, *(r for r in head.functions if r.path != PRICING)])[0] == apply
    assert len(merge_regions([apply], list(head.functions))) == 1 + 5


def test_ingest_a_neutral_bundle(tmp_path: Path) -> None:
    store = local_facts_store(tmp_path / "facts")
    ctx = make_ctx("ts-1")
    bundle = read_any_bundle(NEUTRAL_TS / "bundle")
    result = ingest_shard(store, ctx, bundle, 0, bundle.functions, language="typescript")
    assert result.unmatched_functions == 0 and result.bundle_sha256 == bundle.meta["convert"]["bundle_sha256"]
    finalize_run(
        store, ctx, expected_shards=1, catalogue=NeutralAdapter.from_bundle(bundle).shape_catalogue()
    )
    summary = run_summary(store, ctx.repo_id, ctx.run_id)
    assert summary is not None and summary.partial_branches == 1
    languages = set(store.read_table(ctx.repo_id, ctx.run_id, "functions").column("language").to_pylist())
    assert languages == {"typescript"}
    rate = next(r for r in bundle.functions if r.qualname == "rateFor")
    cov = function_input_cov(store, ctx.repo_id, ctx.run_id, rate.func_key)
    assert cov is not None and cov.tests_truncated == 1
    assert "zone:'domestic'" in cov.missing["zone"]
    shutil.rmtree(tmp_path / "facts")


def test_merge_sampling_keeps_the_cap() -> None:
    func = ("a.ts", "f", 1)
    merged = merge_sampling(
        [FuncSampling(func, 3, ("t1",), False), FuncSampling(func, 5, ("t2",), True, "full")]
    )
    assert merged[func] == FuncSampling(func, 8, ("t1", "t2"), True, "full")
