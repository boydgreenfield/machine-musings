# Invariants

An invariant is a behavioural rule a repository declares about its own code: "allocating cents never gains or loses a cent", "a signup on a customer's domain is refused". Tests and properties check it; coverset reports, on every run, whether it held, whether anything checked it at all, and which kinds of input the checking tests reached.

Rules live in `invariants.yaml` at the repository root (`[tool.coverset] invariants` moves it). Tests link to them with `@pytest.mark.invariant("<id>")`. The code is `src/coverset/analysis/invariants.py`; the orchestrator evaluates every invariant after each run (`orchestrator/verdict.py`).

## Statuses

| Status | Meaning | When it appears |
|---|---|---|
| `satisfied` | the rule held | at least one linked test (or Hypothesis test, for `property`) ran and none failed; or, with only `review` entries, every review finding for the current body says satisfied; or carried forward (below) |
| `violated` | the rule was broken | a linked test failed, errored, or is marked `xfail` (an `xfail` on a linked test documents a known violation) |
| `untested` | nothing checked it | no linked test ran, or the only check is `runtime` |
| `stale` | it held before, but the code or the rule changed since | no fresh evidence this run, and the last `satisfied` result was for a different target body hash or spec hash |
| `needs_review` | only a review can decide | the invariant has only `review` entries and no finding exists for the target's current body |

`test` and `property` entries decide first: violated beats satisfied beats untested. `review` entries are advisory. They can move an invariant out of `needs_review` to `satisfied`, but they never override a test or property outcome and never produce `violated`.

**Carry forward.** When a run leaves an invariant `untested` (or `needs_review` with no review finding), coverset looks at the latest earlier result of the same invariant that is real evidence: `stale` rows are skipped, because they are derived, and within one PR run the row of the same spec wins over its `(base)` row (`orchestrator/verdict.py`, `_previous_result`). If that was `satisfied` and both the target's `body_hash` and the invariant's `spec_hash` are unchanged, the status stays `satisfied` with `carried_from_run` in the evidence. If either changed, the status is `stale` with `stale_since_run` naming that satisfied run and a reason such as "target body changed" (`test_stale_and_carried_forward`). So `stale` persists from run to run until fresh evidence arrives, instead of decaying to `untested` on the run after (`tests/orchestrator/test_review_fixes.py::test_previous_result_skips_stale_rows`, `test_stale_is_carried_forward`); a body changed back to the satisfied one is `satisfied` again.

## The `invariants.yaml` schema

The file is validated against a JSON Schema (draft 2020-12, `INVARIANTS_SCHEMA`). Unknown keys are errors everywhere. A bare YAML list of invariants is accepted as shorthand for `{invariants: [...]}`; an empty file means no invariants.

| Key | Type and limits | Required | Meaning |
|---|---|---|---|
| `version` | `1` | no | format version |
| `invariants` | list, at most 1,000 | yes | the rules |
| `invariants[].id` | `^[A-Za-z_][A-Za-z0-9_.-]{0,127}$`, unique in the file | yes | the stable identity, used as `inv_key` |
| `scope.kind` | `function`, `class`, `module`, `package` or `endpoint` | yes | what the rule is about |
| `scope.target` | string, 1 to 512 characters | yes | dotted `module.qualname` for `function` and `class`, dotted module for `module`; free text for `package` and `endpoint` |
| `statement` | string, 1 to 4,000 characters | yes | the rule in prose (EARS style, "When ..., the ledger shall ...", encouraged) |
| `severity` | `error`, `warning` or `info` | no, default `error` | how bad a violation is |
| `verify` | list of 1 to 32 entries | yes | how the rule is checked |
| `verify[].kind` | `test`, `property`, `runtime` or `review` | yes | see below |
| `verify[].marker` | same pattern as `id` | no | the `@pytest.mark.invariant` value that links tests; default: the invariant's `id` |
| `verify[].test_ids` | up to 256 node ids or prefixes, each up to 512 characters | no | link tests by id instead of by marker |
| `verify[].check` | a Python expression, up to 2,000 characters | no | for `runtime`; syntax-checked, never evaluated |
| `examples` | up to 100 strings | no | values or calls the rule allows |
| `counterexamples` | up to 100 strings | no | values or calls that would break it |
| `rationale` | string | no | why the rule exists |
| `owner` | string, up to 255 characters | no | who to ask |

`resolve_target` turns `scope.target` into a function region: it strips `[tool.coverset] source_root` from each region's path, turns the path into a module name, and matches `module.qualname`. When several regions share the qualname, the first one with a real body wins: `@overload` stubs (a body of only `...`, `pass` or a docstring, `analysis/functions.is_stub`) are skipped while the implementation exists, so the target's body hash, covering tests and lens are the implementation's (`test_an_overloaded_target_is_its_implementation`). Only `function`, `class` and `module` scopes resolve; `package` and `endpoint` invariants are evaluated without a target, so they get no coverage lens and no body hash.

### An annotated example

This is the first entry of `examples/demo-app/invariants.yaml`, with comments added:

```yaml
version: 1
invariants:
  - id: cents_conserved                     # inv_key; tests link to it by this name
    scope:
      kind: function
      target: ledger.billing.allocate.allocate_cents   # src/ledger/billing/allocate.py, source_root "src"
    statement: >-
      When allocate_cents splits an integer total across a non-empty list of
      positive finite weights, the ledger shall return integer shares whose sum
      equals the total exactly, for refunds (negative totals) as well as charges.
    severity: error                         # the default; lowering it in a PR counts as weakening
    verify:
      - kind: test                          # tests marked @pytest.mark.invariant("cents_conserved")
        marker: cents_conserved
      - kind: property                      # the same marker, Hypothesis tests only
    examples:                               # shown to reviewers; never executed
      - "allocate_cents(100, [1, 1, 1]) == [34, 33, 33]"
      - "allocate_cents(7, [1, 1, 1, 1]) == [2, 2, 2, 1]"
      - "sum(allocate_cents(-100, [1, 2])) == -100"
    counterexamples:
      - "allocate_cents(100, [1, 1, 1]) == [33, 33, 33] (a cent is lost)"
      - "allocate_cents(-100, [1, 2]) == [-32, -66] (two cents appear from nowhere)"
    rationale: >-
      Invoice lines, discounts and refunds are reconciled against the payment
      processor to the cent; a split that gains or loses a cent leaves the books
      out of balance and shows up as an unexplained difference at month end.
    owner: team-billing
```

The file declares three more: `durations_consume_whole_input` (`property`), `merge_idempotent` (`property`, severity `warning`) and `signup_rejects_existing_domain` (`test` plus `review`). `uv run coverset invariants check --repo examples/demo-app` validates the file and prints the four as a table.

## Linking tests: `@pytest.mark.invariant`

pytest-coverset registers the marker, so `--strict-markers` accepts it without configuration:

```python
import pytest

@pytest.mark.invariant("cents_conserved")
def test_refund_partial():
    ...
```

`@pytest.mark.invariant(id="cents_conserved")` works too. A test may carry several markers; class and module markers apply to every test inside. The plugin writes each test's ids to `collect.json` (`invariant_ids`) and to `invariant_links.jsonl` ([docs/instrumentation.md](instrumentation.md#the-bundle)).

A `test` or `property` entry links tests in one of two ways:

| Entry has | Linked tests |
|---|---|
| `test_ids` | every collected node id equal to an entry, a parametrisation of it (`<id>[...]`), or inside it (`<id>::...`, so a file or class prefix works) |
| no `test_ids` | every test whose markers include the entry's `marker`, or the invariant's `id` when `marker` is absent |

## Evaluation per verify kind

| Kind | Evaluated as | Result |
|---|---|---|
| `test` | the linked tests that ran (`passed`, `xpassed`, `xfailed`, `failed`, `error`; `skipped` and `not_run` do not count) | `violated` if any failed, errored or xfailed; `satisfied` if some ran and none did; else `untested`, "no linked test ran" |
| `property` | the same, restricted to Hypothesis tests (`collect.json` `hypothesis: true`: a `@given` test) | the same; `untested` reads "no linked Hypothesis test ran" |
| `runtime` | nothing in this version | `untested`, "runtime checks not implemented" |
| `review` | a review finding for the target's current `body_hash`: from this run's AI review, else from stored findings | `satisfied` when the finding says satisfied, else `needs_review`; advisory |

Test `test_xfailed_linked_test_is_a_known_violation` covers the `xfail` rule, `test_property_requires_hypothesis_tests` the property filter, `test_test_ids_select_by_nodeid_prefix` the id matching and `test_review_entries_are_advisory` the review rule.

Each result is stored in `invariant_results` with the status, `target_func_key`, `target_body_hash`, the covering test ids and an `evidence` object that holds one entry per verify item plus the coverage lens. A changed spec is a new `invariants` row (a new `spec_hash`), so history per version is kept.

On the demo, after a local run, the stored statuses are:

| Invariant | `main-6` | PR #1 (refunds) | PR #2 (subdomains) |
|---|---|---|---|
| `cents_conserved` | satisfied | violated (`test_refund_partial` failed) | satisfied |
| `durations_consume_whole_input` | satisfied | satisfied | satisfied |
| `merge_idempotent` | satisfied | satisfied | satisfied |
| `signup_rejects_existing_domain` | untested | untested | untested |

No demo test carries the `signup_rejects_existing_domain` marker, on purpose, so that invariant is `untested` everywhere (`examples/demo-app/tests/README.md`). On PR #2 the fixture AI review records a finding that says violated, with the counterexample `check_if_new_user_signup_allowed('bob@eng.acme.com', {'acme.com'})` returning True; the status stays `untested`, because a review never yields `violated`.

## The coverage lens

Each result carries a one-line lens (`invariant_lens`): how many tests are linked, which of the target's expected input classes those tests never reached, and whether they cover the target's changed lines. From the same demo run:

```
cents_conserved: 6 tests, never reached total_cents:negative, weights:empty, weights:zero-element, weights:negative-element, weights:all-zero, weights:nan-element
merge_idempotent: 1 test, never reached intervals:empty, intervals:unsorted, intervals:duplicates, intervals:inverted-pair
```

The first line is `main-6`: the six tests that check conservation never pass a negative total, which is exactly the class PR #1's refund code adds. On PR #1 the lens has 7 tests and `total_cents:negative` is no longer in the list, because the new refund test reaches it and fails. A lens lists classes the *linked* tests missed, even when some other test reached them. When the target changed in the PR and no linked test exists, it ends with "changed lines not covered by linked tests". "Changed lines" follow the body-line rule of [docs/data-model.md](data-model.md#the-body-line-rule): only changed body statements are asked about, because a test never records a `def`, decorator or docstring line; a signature-, decorator- or docstring-only change asks whether a linked test ran any line of the body (`test_signature_and_decorator_changes_ask_about_the_body`).

The input classes come from the catalogue described in [docs/input-classes.md](input-classes.md). Invariant target files are always on the full-trace list, so their classes are never sampled away.

## Invariants changed by a PR

For a PR run coverset loads `invariants.yaml` from both the merge-base and the head (`orchestrator/verdict.py`) and compares them (`diff_invariants`):

| Change | Rule |
|---|---|
| `removed` | the id is in the base and not in the head |
| `weakened` | severity lowered; scope changed; or a base `verify` entry has no head entry with the same kind, marker and check and a superset of its `test_ids` |
| `changed` | any other spec difference (statement, examples, rationale, ...) |
| `added` | the id is only in the head |
| `unchanged` | equal spec hashes |

Every change other than `unchanged` appears in the verdict under "Invariants changed by this PR", with its reasons (`test_diff_invariants`). Base definitions that the head **removed** are still evaluated in that run, with `removed_in_head` in their evidence, so deleting a rule does not silence it on the PR that deletes it. A **weakened** rule is evaluated twice: in its head form, and in its base form, stored as a row of its own labelled `<id> (base)` with `base_definition` and `weakened_in_head` in its evidence, so the stricter rule the PR relaxed still reports whether it held (`test_weakened_invariant_base_definition_is_evaluated`).

An `invariants.yaml` that fails to load is logged and treated as empty for that side.

## Review invariants and `coverset invariants`

A `review` entry asks the advisory AI reviewer ([docs/ai-review.md](ai-review.md)) to judge the rule against the target's current body. Findings are stored in `review_findings` keyed by `(func_key, bundle_hash)` and reused while the function, its tests and its facts are unchanged. A finding counts only for the body hash it was made on: once the body changes, the invariant is back to `needs_review` (or `stale`).

| Command | Status |
|---|---|
| `coverset invariants check [--repo PATH]` | built: loads and validates a checkout's `invariants.yaml` with the repo's `[tool.coverset]` and prints a table (`commands/maintenance.py`; test `test_invariants_check`) |
| `coverset invariants scan [--repo PATH\|NAME] [--executor local\|modal]` | built: reviews every invariant with a `review` entry at the head of the latest completed, trusted baseline run of the default branch, through the configured review client (the offline fake unless `COVERSET_AI=anthropic` and the repo opted in). Findings land in `review_findings` under that run and the invariant's row is rewritten with `trigger = 'scan'`: review-only invariants take the review's status (`satisfied` or `needs_review`), mixed invariants keep the status their tests decided and gain the review as evidence; a review never yields `violated`. It also runs automatically, in the background, after every trusted, completed (`success` or `failure`) baseline of the default branch whose head is still the branch tip, pinned to that run (`COVERSET_SCAN_AFTER_BASELINE`, default true; with `COVERSET_AI=off` it still records the statuses). Errors are logged and never affect the run (`commands/invariants.py`, `orchestrator/runner.py` `scan_after_baseline`; tests in `tests/orchestrator/test_invariants_scan.py` and `tests/orchestrator/test_review_fixes.py`). |

## Runtime checks (designed, not built)

`verify.kind: runtime` with a `check` expression is accepted and validated today, and always reported `untested`. The plan names the mechanism, local `PY_START` and `PY_RETURN` events; the steps below are the intended shape, not a specification. The design reuses the shape tracer's machinery ([docs/instrumentation.md](instrumentation.md#discovery-a-global-py_start-then-local-events)), inside the worker, never in the control plane:

1. The plugin receives the runtime checks of the run's invariants, with their resolved targets, in a file next to the full-trace list.
2. For each target code object it enables local `PY_START` and `PY_RETURN` events, exactly as for a full-trace function (no sample cap).
3. On `PY_START` it binds the parameters from the frame; on `PY_RETURN` it adds `result`.
4. It evaluates the compiled check in a namespace with only those names, under the tracer's safety rules (re-entrancy guard, every exception caught and counted).
5. It records per invariant the number of calls checked, the number that failed, and the redacted arguments of the first failure, in a new bundle file.

The orchestrator would then report `violated` with the failing call, `satisfied` when checked calls all passed, and `untested` when no call reached the target. The check runs the PR's own expression inside the PR's own process, so it carries the same trust as the PR's tests: it is evidence, not a security control.

## Security

`invariants.yaml` comes from the repository, so a pull request can change it. The loader treats it as untrusted data (`load_invariants`):

| Control | Test |
|---|---|
| at most 1 MiB, checked before parsing | `test_size_cap` |
| parsed with a `yaml.SafeLoader` subclass: no Python tags such as `!!python/object` | `test_python_object_tags_are_refused` |
| YAML aliases refused, so alias bombs cannot blow up validation | `test_alias_bombs_are_refused` |
| schema validation with `additionalProperties: false` and length caps | `test_invalid_documents_are_rejected` |
| `check` expressions only `ast.parse(expr, mode="eval")`-checked; the control plane never calls `eval`, `exec` or `compile` on them | `test_check_expressions_are_never_evaluated` |

`test_python_object_tags_are_refused`, `test_alias_bombs_are_refused` and `test_check_expressions_are_never_evaluated` carry `@pytest.mark.security`. Invariant text reaches the PR comment only through the markdown escaping described in [docs/github.md](github.md#what-the-app-posts).
