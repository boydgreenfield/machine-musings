"""The live pages on a TypeScript run: input classes from the descriptors stored at ingest.

The TypeScript fixture is ingested as a run of a synthetic repository with the end-to-end test's
helper (``tests/e2e/test_neutral_ingest.py``). The function page and the PR page's cards build their
classes with :meth:`RunFacts.function_view`; for a run whose functions shipped type descriptors it
must use those (``functions.types_json``), not parse the TypeScript source as Python, so the page
counts exactly the classes ``finalize_run`` stored in ``func_input_cov``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

from test_neutral_ingest import FIXTURE, RATES, REPO, RUN, ingest_ts_fixture  # noqa: E402

from coverset.analysis.shapes import function_catalogue  # noqa: E402
from coverset.facts import queries  # noqa: E402
from coverset.web.facts_view import RunFacts  # noqa: E402

PRICING_SRC = "src/cart/pricing.ts"


@pytest.fixture(scope="module")
def facts(tmp_path_factory: pytest.TempPathFactory) -> RunFacts:
    ingested = ingest_ts_fixture(tmp_path_factory.mktemp("facts"))
    return RunFacts(ingested["store"], REPO, RUN)


def _region(facts: RunFacts, qualname: str):  # noqa: ANN202
    return next(r for r in facts.regions if r.qualname == qualname)


def _source(path: str) -> str:
    return (FIXTURE / "repo" / path).read_text(encoding="utf-8")


def test_the_run_is_typescript_with_stored_descriptors(facts: RunFacts) -> None:
    assert facts.language == "typescript"
    rate = _region(facts, "rateFor")
    assert facts.declared_types[rate.func_key]["zone"].values == ("domestic", "eu", "world")
    # What the page did before: the Python parser finds no annotation in a .ts file.
    assert function_catalogue(rate, _source(RATES)).annotations == {}


def test_the_function_page_lists_the_literal_classes(facts: RunFacts) -> None:
    rate = _region(facts, "rateFor")
    view = facts.function_view(rate, _source(RATES))
    expected = {c.name for c in view.cov.expected}
    assert {"zone:'domestic'", "zone:'eu'", "zone:'world'", "weightsGrams:zero-element"} <= expected
    assert "zone:'eu'" in view.cov.observed
    assert {"zone:'domestic'", "zone:'world'"} <= {c.name for c in view.cov.missing}
    assert view.annotations["zone"] == "Literal['domestic', 'eu', 'world']"
    zone = next(row for row in view.grid().rows if row.param == "zone")
    assert {cell.name for cell in zone.cells} >= {"zone:'domestic'", "zone:'eu'", "zone:'world'"}


def test_the_function_page_lists_the_enum_members(facts: RunFacts) -> None:
    fmt = _region(facts, "formatPrice")
    view = facts.function_view(fmt, _source(fmt.path))
    assert "currency:Currency.EUR" in view.cov.observed
    assert "currency:Currency.USD" in {c.name for c in view.cov.missing}


@pytest.mark.parametrize("qualname", ["rateFor", "formatPrice", "applyDiscount"])
def test_the_cards_count_what_finalize_stored(facts: RunFacts, qualname: str) -> None:
    region = _region(facts, qualname)
    stored = queries.function_input_cov(facts.store, REPO, RUN, region.func_key)
    assert stored is not None
    for source in (_source(region.path), None):  # the PR page passes the head source when it has it
        view = facts.function_view(region, source)
        assert len(view.cov.expected) == stored.blocks_expected
        assert view.cov.observed_expected == stored.blocks_observed
        stored_missing = {name for names in stored.missing.values() for name in names}
        assert {c.name for c in view.cov.missing} == stored_missing
