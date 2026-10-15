# What these tests leave out on purpose

The five functions the coverset README discusses are at 100% line and branch coverage:
`allocate_cents`, `parse_duration`, `validate_email`, `check_if_new_user_signup_allowed` and
`merge_intervals`. Their tests, including the ones that reach them indirectly through invoices,
expiry windows, signup and overlap totals, never pass in these kinds of input:

| Function | Input classes no test passes in |
|---|---|
| `validate_email` | a multi-label domain such as `bob@eng.acme.com`, an internationalised domain such as `bob@bücher.de` |
| `check_if_new_user_signup_allowed` | an address on a subdomain of an existing customer's domain |
| `parse_duration` | a multi-letter unit (`100ms`), decimals (`1.5h`), signs (`-5m`), trailing text (`5m later`) |
| `allocate_cents` | a negative total, a zero weight, an empty or zero-sum weight list, NaN or infinite weights |
| `merge_intervals` | unsorted, touching (`(1, 3), (3, 5)`), nested, inverted (`(5, 1)`) or empty input |

The Hypothesis property tests in `test_properties.py` draw from strategies that stay inside the
same classes, so they pass on `main` and still never reach the gaps.

The helpers below are deliberately untested or only partly tested, so the coverage treemap has
texture:

- `ledger.util.text.initials`: untested; `truncate` never sees a width smaller than its placeholder.
- `ledger.util.money.parse_amount`: never sees a non-finite amount such as `"NaN"`.
- `ledger.util.dates.add_business_days` and `quarter_of`: untested.
- `ledger.accounts.roles.can_manage_billing`: untested.
- `ledger.billing.invoices.render_invoice`: untested.
- `ledger.durations.format.humanize`: never formats a value under a minute.
- `ledger.intervals.overlap.gaps`: untested.

Four tests sleep for 0.3-0.8 s (their names end in `_slow`) so sharding has uneven durations to
balance; their assertions do not depend on the wall clock. `[tool.coverset] target_shard_seconds = 1`
makes the roughly 2.5 s suite fan out over three shards.

## The flaky test

`test_hold_survives_clock_skew` is the one deliberately flaky test. It fails exactly when
`int(COVERSET_DEMO_RUN) % 3 == 1` and passes otherwise, including when the variable is unset. Whoever
runs the demo owns the variable, and must set it so the orchestrator's retry sees a pass:

- A history run sets `COVERSET_DEMO_RUN` to its ordinal (main-1 is 1, ..., main-6 is 6). The test
  first exists in main-3, so of main-3..main-6 only main-4 (ordinal 4) fails it.
- The retry exec of a failed test sets `COVERSET_DEMO_RUN` to the ordinal plus one, so the retry
  passes and the failure is recorded as flaky rather than as a confirmed failure.
- Pull request runs leave `COVERSET_DEMO_RUN` unset, so the flaky test never adds a second failure to
  PR #1 or turns PR #2 red.

No test carries `@pytest.mark.invariant("signup_rejects_existing_domain")`, on `main` or in PR #2, so
that invariant is `untested` on every run instead of satisfied on `main` and then stale on PR #2.
