# Support partial refunds

Customers can now be refunded part of a paid invoice. `ledger.billing.refund.refund()` takes the
refund as a negative amount, the way refunds post to the ledger, and splits it across the invoice
lines in proportion to their original amounts by reusing `allocate_cents`, so refunds round
exactly like charges do.

- Add `ledger/billing/refund.py` with `refund(total_cents, weights)`; positive totals are rejected.
- Add `tests/test_refund.py`; `test_refund_partial` is linked to the `cents_conserved` invariant.
