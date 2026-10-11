# Synthetic TypeScript neutral bundle

**Synthetic.** No TypeScript was compiled or run to make this. The repository in `repo/` is
invented (three files, eight functions) and the bundle in `bundle/` is what a Vitest reporter that
follows [`docs/neutral-bundle.md`](../../../docs/neutral-bundle.md) would write for twelve tests
over it. It is written by `scripts/make_neutral_fixture.py` (deterministic: fixed timestamps,
gzip without mtime, sorted rows), so `uv run python scripts/make_neutral_fixture.py` rewrites the
same bytes and `--check` says whether the committed copy is stale.

| Path | What it is |
|---|---|
| `repo/src/cart/pricing.ts` | `enum Currency`, `applyDiscount(totalCents, percent)`, `formatPrice(cents, currency)`, `Cart.addItem(sku, qty)` |
| `repo/src/util/strings.ts` | `slugify(title)`, `parseQuantity(text?)`, `pad(value, width)` |
| `repo/src/shipping/rates.ts` | `rateFor(zone, weightsGrams)`, `isFreeShipping(totalCents)` |
| `bundle/manifest.json` | `language: "typescript"`, `producer: vitest-coverset-fixture 0.0.1`, shard 0 |
| `bundle/tests.jsonl` | 12 tests: 8 passed, 2 failed (`formatPrice > formats euros`, `pad > pads decimals`), 1 skipped, 1 never ran (`outcome: null`); one property test |
| `bundle/files.jsonl` | statements and possible branch arcs of the three files |
| `bundle/coverage.jsonl` | per-test executed lines, plus an `<import>` row per file (the declarations) |
| `bundle/arcs.jsonl` | executed arcs; `pricing.ts` line 7 (`if (percent <= 0)`) is a **partial branch**: no test passes a zero discount |
| `bundle/functions.jsonl` | the eight regions with type descriptors: `int`, `float`, `str`, a union (`pad.value`), an optional (`parseQuantity.text`), a literal (`rateFor.zone`), an enum (`formatPrice.currency`) and a list of `int` (`rateFor.weightsGrams`) |
| `bundle/shapes.jsonl` | observations with redacted examples, and one `sampling` row per traced function; `rateFor` hit the adaptive cap (`cap: "sample"`, 2048 calls) |
| `bundle/links.jsonl` | two invariant links (`discount_never_negative`, `shipping_cost_monotone`) |
| `bundle.tar.gz` | the same files as a flat archive (`coverset_model.io.pack`) |

What the tests read from it (`tests/e2e/test_neutral_ingest.py`, `tests/model/`):

- `applyDiscount`'s `totalCents:negative` is never exercised (no test passes a negative total, and the
  function was not capped), `formatPrice`'s `currency:Currency.USD` is never exercised, and
  `rateFor`'s `zone:'domestic'` is "not observed in 2048 sampled calls (1 test hit the sample cap)".
- `isFreeShipping` was never called: its only test never ran.
- Line 7 of `pricing.ts` is covered by both `applyDiscount` tests and is a partial branch.
