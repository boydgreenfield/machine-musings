"""The shape grid of a function no argument shape was recorded for.

A Rust bundle (and a Python run without shape capture) records no call shapes, yet its tests ran
the functions: the grid must say "called by N tests, no argument shapes recorded", and keep "never
called by a test" for functions line coverage says no test ran. The neutral-rust and neutral-ts
fixtures go through the orchestrator's ingest into a scratch facts store, and the grid comes from
:meth:`RunFacts.function_view` as the web pages build it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from coverset.facts.ingest import RunContext, finalize_run
from coverset.facts.store import FactsStore
from coverset.facts.untrusted import BundleLimits
from coverset.lang import adapter_for_language
from coverset.orchestrator.bundles import IngestJob, ingest_one
from coverset.web.facts_view import FunctionView, RunFacts

ROOT = Path(__file__).resolve().parents[2]
REPO, RUN = 21, "polish-run-1"


def ingest(tmp: Path, language: str) -> tuple[RunFacts, Path]:
    fixture = ROOT / "fixtures" / "bundles" / ("neutral-rust" if language == "rust" else "neutral-ts")
    tree = fixture / "repo"
    head = frozenset(str(p.relative_to(tree)) for p in tree.rglob("*") if p.is_file())
    ctx = RunContext(REPO, RUN, "a" * 40, "b" * 40, "main", 3, datetime(2026, 10, 14, tzinfo=UTC), None)
    job = IngestJob(
        facts_dir=str(tmp),
        ctx=ctx,
        shard_id=0,
        tarball=str(fixture / "bundle.tar.gz"),
        limits=BundleLimits(),
        source_root=str(tree),
        head_paths=head,
        functions=(),
        keep_paths=frozenset(),
        language=language,
    )
    outcome = ingest_one(job)
    assert outcome.ok, outcome.error
    store = FactsStore(tmp)
    finalize_run(store, ctx, expected_shards=1, catalogue=adapter_for_language(language).shape_catalogue())
    return RunFacts(store, REPO, RUN), tree


def views(facts: RunFacts, tree: Path) -> dict[str, FunctionView]:
    out = {}
    for region in facts.regions:
        source = tree / region.path
        out[region.qualname] = facts.function_view(region, source.read_text() if source.is_file() else None)
    return out


@pytest.fixture(scope="module")
def rust(tmp_path_factory: pytest.TempPathFactory) -> dict[str, FunctionView]:
    facts, tree = ingest(tmp_path_factory.mktemp("rust-facts"), "rust")
    return views(facts, tree)


@pytest.fixture(scope="module")
def typescript(tmp_path_factory: pytest.TempPathFactory) -> dict[str, FunctionView]:
    facts, tree = ingest(tmp_path_factory.mktemp("ts-facts"), "typescript")
    return views(facts, tree)


def notes(view: FunctionView) -> dict[str, str]:
    return {row.param: row.note for row in view.grid().rows}


def test_rust_functions_ran_by_tests_are_not_called_never_called(rust: dict[str, FunctionView]) -> None:
    allocate = rust["allocate_cents"]
    assert allocate.observations == () and allocate.tests_called == 4
    assert notes(allocate) == {
        "total_cents": "called by 4 tests, no argument shapes recorded",
        "weights": "called by 4 tests, no argument shapes recorded",
    }
    transfer = rust["Ledger::transfer"]
    assert transfer.tests_called == 1
    assert set(notes(transfer).values()) == {"called by 1 test, no argument shapes recorded"}
    every = [n for v in rust.values() for n in notes(v).values()]
    assert every and "never called by a test" not in every, "every fixture function ran under a test"


def test_typescript_keeps_never_called_for_what_no_test_ran(typescript: dict[str, FunctionView]) -> None:
    unused = typescript["isFreeShipping"]
    assert unused.observations == () and unused.tests_called == 0
    assert notes(unused) == {"totalCents": "never called by a test"}
    # functions with shapes keep their cells; the covering-test count is not even read for them
    apply = typescript["applyDiscount"]
    assert apply.observations and apply.tests_called is None
    assert all(row.cells for row in apply.grid().rows if row.param != "return")


def test_the_note_without_line_coverage_counts() -> None:
    """A view built without the count (the static report's older callers, unit tests) keeps the
    old wording; zero tests is "never called"."""
    view = rust_like_view(None)
    assert view.no_shapes_note == "never called by a test"
    assert rust_like_view(0).no_shapes_note == "never called by a test"
    assert rust_like_view(1).no_shapes_note == "called by 1 test, no argument shapes recorded"
    assert rust_like_view(12).no_shapes_note == "called by 12 tests, no argument shapes recorded"


def rust_like_view(tests_called: int | None) -> FunctionView:
    from coverset.analysis.functions import index_functions
    from coverset.analysis.shapes import Catalogue

    source = "def check(email, tries):\n    return email\n"
    region = next(r for r in index_functions("m.py", source) if r.qualname == "check")
    cov = Catalogue().function_input_cov(region, [], None, annotations={})
    return FunctionView(region, cov, (), {}, {}, None, (), 100.0, None, tests_called=tests_called)
