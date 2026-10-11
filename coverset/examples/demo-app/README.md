# ledger

The demo target for coverset: a small billing ledger split into four packages.

- `ledger.billing`: largest-remainder cent allocation, invoices with discounts, regional tax.
- `ledger.durations`: parsing and formatting `1h30m`-style durations, expiry windows.
- `ledger.accounts`: email validation, the self-signup rule, account roles.
- `ledger.intervals`: merging and intersecting integer intervals.
- `ledger.util`: text, money and date helpers.

Every function the coverset README discusses (`allocate_cents`, `parse_duration`, `validate_email`,
`check_if_new_user_signup_allowed`, `merge_intervals`) is at 100% line and branch coverage, and the tests
still never pass in several kinds of input; `invariants.yaml` declares the rules those functions must keep.
See `tests/README.md` for what is deliberately left untested.

```
uv run python -m pytest
```
