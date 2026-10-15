# Verification records

The core object of coverset becomes the **verification record**: a statement that one test verified a set of function bodies, at known content hashes, under a known environment, observing known input classes, with known reliability. Records are derived from runs and keyed by content, so they outlive the run that produced them, are invalidated exactly by the diffs that touch what they cover, and can be queried by people and agents without re-running anything. Status: **designed, not built**; this page is the design the build follows.

## Why

Today the unit is a run. A run's facts say which tests ran and what they covered, and a PR's verdict is computed against a prior run chosen by heuristic (previous head, merge-base, latest baseline). Three things follow from that choice and all three get better with records:

| Today | With records |
|---|---|
| Wave 1 approximates the impacted tests from the prior run's coverage. | The impacted tests are exactly the records a diff invalidates. |
| Every collected test runs on every PR; selection was refused because the risk score is uncalibrated. | Tests whose records are still valid need not run; skipping is justified by content identity, not prediction, and the residual risk is named. |
| "Confidence" could only be a prediction. | Verification progress is a measurement: the share of invalidated records re-established at second t, and what is still pending. |

Post-merge verification falls out too: the default branch's head carries a set of records; each merge invalidates some; "pending verification" is the number of merged changesets whose invalidated records are not yet re-established. That replaces pairwise verification of PR combinations with continuous verification of the integrated branch.

## The record

One row per (test, function) pair the test executed, written at finalize from facts that already exist.

| Field | Source | Meaning |
|---|---|---|
| `test_id`, `nodeid` | `tests_dim` | the test |
| `func_key`, `path`, `qualname` | `functions` | the function whose body the test executed (a line of `[body_start, end_line]` is in the test's footprint; `func_body`) |
| `body_hash` | `functions` | the function body's content hash at the verified revision (formatting-insensitive) |
| `sig_hash` | `functions` | the signature's hash (a changed signature invalidates like a changed body) |
| `env_hash` | `runs.env_hash` | the locked environment the test ran in |
| `config_hash` | new: sha256 over the repository's `tracked_globs` (`uv.lock`, `conftest.py` files, `pyproject.toml`'s `[tool.coverset]` and `[tool.pytest]`, `invariants.yaml`) at the verified revision | the fixtures-and-configuration the test depended on that are not function bodies |
| `classes` | `call_shapes` joined to the catalogue | the input classes the test passed into this function (per parameter) |
| `outcome` | `test_results` | `passed` only produces a record; a failed or errored test produces none |
| `flaky`, `flaky_score` | `test_results`, `test_stats` | reliability of the evidence; a pass after a retry is recorded with `flaky = true` |
| `run_id`, `head_sha`, `verified_at` | `runs_dim` | the evidence |
| `shard_complete` | `runs.coverage_complete` | false when the run's coverage was partial (a shard was lost); such records are weaker and are refreshed first |
| `evidence` | new | `tracer` for in-process records (the only kind in the build plan); `trace` for records derived from OpenTelemetry spans of an end-to-end test (see Extensions) |

A record is **valid at revision R** when, at R, the function exists with the same `func_key`, the same `body_hash` and `sig_hash`, every other function in the same test's footprint also has unchanged hashes, and `env_hash` and `config_hash` are unchanged. The unit of validity is the test's whole footprint, not one function: a test verifies the behaviour of the code it executed, so a change anywhere in that code invalidates everything the test vouched for. Records of one test therefore invalidate together; storing them per function is what lets a diff find them.

**Not covered by the hash, and therefore stated, not hidden:** module-level code (imports, constants) is covered through its own `<module>` region with a body hash of the module-level statements; data files, templates and anything a test reads from disk outside `tracked_globs` are not in the key, nor are external services, time and randomness. A record is "verified, assuming unchanged inputs outside the repository"; the PR page says so once, in the same place the file-order estimate is labelled an estimate.

## Invalidation by a diff

Given base and head (or main's previous head and new head after a merge), the diff classifier already yields changed functions with `kind` in `added | deleted | modified | trivial | renamed | moved`. Invalidation:

- `modified`, `deleted`, `added` (with a new body hash) invalidate every record whose footprint contains that `func_key`.
- `trivial` (comments and formatting only; same `body_hash`) invalidates nothing: that is what the hash is for, and it replaces today's "inherited" label with something true.
- `renamed` and `moved` with an unchanged `body_hash` keep their records under the new `func_key` (a rename is a key change, not a behaviour change); a renamed-and-edited pair invalidates.
- A changed `env_hash` (lockfile) or `config_hash` invalidates every record of the repository.
- A changed test file invalidates the records of the tests it defines (the test itself changed), plus any test that imports from it (conftest, helpers) through `tracked_globs`.

The result is three sets per revision: **invalidated** (must run), **valid** (carried forward from the last verified revision), **unverified** (functions at the new revision with no record at all: new code, or code no test ever reached). The verdict and the pages speak in these terms.

## What runs

The planner replaces its prior-run heuristic with the invalidated set:

1. Invalidated tests run first on every shard (today's wave 1), then tests with no history, then the rest.
2. `[tool.coverset] carry_forward = false` (the default until the first repository has a few hundred runs of records): every collected test still runs; valid records are reported as carried forward, and the run re-establishes them, so the saving is reporting, not time. The page shows what would have been skipped and how much time it took, so the policy is measured before it is trusted.
3. `carry_forward = true`: tests whose records are valid at head are not executed; their records are carried into the new run's facts with `carried_from = <run_id>`; the comment says "N tests carried forward (verified at <sha7>, unchanged since)". A flaky record is never carried forward; a record from an incomplete run is never carried forward; a test that has not been executed for `carry_forward_max_days` (default 7) runs regardless, so drift outside the hash is bounded.
4. Baselines of the default branch always run everything: they are where records are re-grounded.

## What the pages say

**PR page and comment.** The verdict band becomes: "Invalidated 48 records (31 tests) · re-established 31 of 31 · 2 changed functions never verified · 5 input classes never passed in · carried forward 184 tests". Needs-attention cards are ordered: never-verified changed functions first (no record at any revision), then invalidated-and-failed, then re-established with classes missing. "Verification pending" replaces "running" while invalidated records remain.

**Repository page.** Three tiles: changed-code coverage over the last week with its delta (from `file_cov` of baselines), time to first signal with its delta (from `timeline.first_test` and `first_failure`), and pending verification: the number of default-branch changesets since the last fully re-established revision, with the invalidated records still open. A **verification progress** chart per run: the share of invalidated records re-established against seconds of execution (an exact measurement), and, once the repository has 50 runs with a first failure, a second line: the empirical share of first failures that had surfaced by that second over those runs. The second line is the only thing on the page that is an estimate, and it is labelled as such; no "99% confidence" number is shown anywhere.

**Function page.** The record history: verified at which revisions by which tests with which classes, and the current status (valid, invalidated by `<sha7>`, never verified).

**Invariants** are declared behaviours; their records are the records of their linked tests, so "payment retry idempotency: 3 changesets pending" is an invariant whose linked tests have invalidated records.

## Agents

One new tool, `what_needs_verification(run | revision)`, returning the unverified functions (with their missing input classes and the Hypothesis strategies), the invalidated-and-failed records, and the invariants whose records are pending, ranked by the number of changesets since the function was last verified. The existing `get_function` gains the record status. The playbook "bootstrap tests for a function" starts from this tool.

## Facts and storage

A new per-run table `verification` (one row per record established or carried in that run) written by `finalize_run`, and a per-repository summary `verification_head` (the latest valid record per (test, func_key) with its hashes) rewritten at finalize so that invalidation at plan time is one join against the diff, not a scan of history. Both are Parquet in the blob store like every other fact. Retention keeps `verification_head` beyond the runs it came from, so expiring a run never loses what it verified; the run id in a record may then point at an expired run, which the page shows as "evidence expired, record kept".

Sizes: records are O(tests x functions touched), the same order as `func_body` today (3,571 rows for the 5,099-test repository), so the cost is small.

## Risks and the honest caveats

- **The hash is not the whole input.** Data files, environment variables, services and time are outside it. The bounded carry-forward window, the always-run baselines, and the explicit label keep this honest; a repository can lower `carry_forward_max_days` or list more paths in `tracked_globs`.
- **Dynamic footprints are sampled by what ran.** A test that would execute different functions on a different branch of its own logic (depending on the code under test) has a footprint that may miss functions. The footprint is taken from the last run of the test, and a change in a function the test *did not* execute last time cannot be what the test verified, so this is sound for invalidation; it is a limit on what "verified" covers, not on correctness.
- **Flakiness.** A flaky pass is not evidence; records from retried passes are kept but never carried forward and are shown discounted.
- **Partial runs.** Records from a run with a lost shard are complete for the shards that landed; they are marked and refreshed first.
- **No probability is shown until calibrated.** The progress curve is a measurement; the failure-surfacing curve is empirical and per repository; neither is a model.

## Extensions

Two extensions keep the record as the spine and add evidence kinds and weights to it. Neither is in the build plan below; both are designed here so the record's fields leave room for them.

**Traces as evidence for end-to-end tests.** An end-to-end test that drives several services leaves OpenTelemetry spans in each of them. Spans carrying the `code.function`, `code.filepath` and `code.lineno` attributes are a cross-service footprint: test T executed functions {f} in service A at revision rA and {g} in service B at rB. That is a record with `evidence = trace` (today's in-process records are `evidence = tracer`), a footprint that spans repositories, and hashes taken from each service's deployed revision. Linkage: the test puts its id in W3C baggage; the probe's WSGI and ASGI middleware already read `traceparent`, and the probe's shape keys ride on spans as attributes, so e2e tests get input classes as well. Needed: a receiver (`POST /v1/flows`, designed in [production-flows.md](production-flows.md)), the mapping from span attributes to `func_key` (path and qualname; a span without `code.*` attributes is service-level evidence only), and the rule that trace evidence ranks below tracer evidence on the pages (sampled, async gaps, no branches): "verified in-process by 3 tests, and reached by 2 end-to-end flows".

**Production shapes the classes; records are verified against production.** Expected input classes come from type descriptors plus the kinds observed in tests. Production archetypes (the probe's k-anonymous shape vectors, clustered by call volume) add what the catalogue cannot know: which classes exist at all (an `email:multi-label-domain` seen in 14% of calls becomes an expected class even when no annotation implies it) and how much each matters. Each record then carries a third fact beyond lines and classes: the production archetypes the test's observed shapes cover (exact, superset, partial), and a function reads "verified against 83% of its production call volume". That share is volume-weighted and honest, and it is the right prior for ranking: a function with high production volume and a low verified share comes first in the risk order and in `what_needs_verification`. Matching is by `func_key` (path and qualname), not by body hash, because production runs a different revision than the PR; window drift and the k-anonymity threshold are the caveats, both already in the production-flows design.

## Build plan

1. `verification` and `verification_head` tables, written at finalize from existing facts; `config_hash` from `tracked_globs`; tests on the demo (PR #1 invalidates `allocate_cents`'s records; PR #2's `trivial` reformat invalidates nothing).
2. Planner: invalidation by diff replaces the prior-run heuristic for wave 1; carried-forward reporting with `carry_forward = false`; `carry_forward = true` behind the config key with the bounds above.
3. Pages and comment: the verdict vocabulary, the cards' order, the repository tiles, the progress chart, the function page's record history.
4. `what_needs_verification` in the agent service, MCP and `/v1/agent`.
5. Post-merge: baselines report pending verification for the default branch; the dashboard tile.
6. README's core-features table: rows 1, 2 and 5 restated in these terms, with the calibration curve as the one remaining estimate.
