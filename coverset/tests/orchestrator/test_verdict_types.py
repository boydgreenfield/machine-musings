"""The sticky comment's input classes come from a bundle's declared types when it ships them."""

from __future__ import annotations

from pathlib import Path

from coverset.lang import read_any_bundle
from coverset.orchestrator.bundles import IngestOutcome
from coverset.orchestrator.verdict import _input_coverage

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "bundles" / "neutral-ts" / "bundle"


def test_declared_types_decide_the_classes_in_the_verdict() -> None:
    bundle = read_any_bundle(FIXTURE)
    rate = next(r for r in bundle.functions if r.qualname == "rateFor")
    price = next(r for r in bundle.functions if r.qualname == "formatPrice")
    shapes = tuple(o for o in bundle.shapes if o.func in (rate.triple, price.triple))
    outcome = IngestOutcome(
        shard_id=0,
        ok=True,
        shapes=shapes,
        sampling=tuple(s for s in bundle.sampling if s.func in (rate.triple, price.triple)),
        types={t: dict(bundle.types[t]) for t in (rate.triple, price.triple)},
    )
    covs, _ = _input_coverage({rate.func_key: rate, price.func_key: price}, [outcome], sources={})
    expected = {c.name for c in covs[rate.func_key].expected}
    assert {"zone:'domestic'", "zone:'eu'", "zone:'world'"} <= expected
    assert any(
        name.startswith("currency:Currency.") for name in (c.name for c in covs[price.func_key].expected)
    )
    # without declared types the same function falls back to the observation-derived classes
    plain = IngestOutcome(shard_id=0, ok=True, shapes=shapes, sampling=outcome.sampling)
    covs_plain, _ = _input_coverage({rate.func_key: rate}, [plain], sources={})
    assert "zone:'domestic'" not in {c.name for c in covs_plain[rate.func_key].expected}
