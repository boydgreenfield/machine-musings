# Data model

coverset keeps two stores with different shapes.

| Store | Holds | Size per run | Technology | Code |
|---|---|---|---|---|
| Control-plane database | repositories, PRs, runs, tests, results, events, deliveries, invariants, findings | O(files) + O(tests) rows | Postgres 14+ through SQLAlchemy 2.1 async (asyncpg) | `src/coverset/db/` |
| Facts store | per-test executed lines, branch arcs, function regions, argument shapes, run aggregates | O(tests × lines) rows | immutable Parquet files (zstd, dictionary encoding), queried by in-memory DuckDB | `src/coverset/facts/` |

Per-line coverage and argument shapes never go into SQL, so the SQL store stays small; the Parquet store is written once per shard and never updated in place, so any number of processes can read it while one writes.

## Control-plane database

### Connection and migrations

`COVERSET_DATABASE_URL` is a Postgres URL (default `postgresql+asyncpg://localhost/coverset`; anything else is refused at start-up). Every connection gets a bounded pool, `application_name=coverset`, `timezone=UTC` and TCP keepalives, and the web app's a 30 s `statement_timeout` (`db/session.py`; [postgres.md](postgres.md) has the details).

Migrations are Alembic revisions under `src/coverset/db/migrations/versions/`. `coverset init-db` creates the database when the server has none of its name and migrates it, and `coverset serve` does the same before it opens the port unless `COVERSET_MIGRATE_ON_START=false`; the app's own startup migration runs unless `COVERSET_SKIP_MIGRATIONS=true`. Test `test_migrations_match_models` checks that the revisions produce exactly the models.

| Revision | Adds |
|---|---|
| `0001_initial` | every table |
| `0002` | `runs.network`, `approval_check_id`, `approved_by`, `check_run_id` |
| `0003` | `runs.extra_env`, `retry_increment_keys`, `summary_json` |
| `0004` | `webhook_deliveries.body_sha256` (unique), `runs.approval_requested_by`, `pull_requests.author` |
| `0005` | `runs.claimed_by` (`<role>@<host>:<pid>:<nonce>` of the process executing the run), `runs.heartbeat_at` (refreshed every 10 s while it executes), `runs.rank_executed` (the position at which the first confirmed failure's first attempt reported: how many first-attempt results, across all shards, had arrived when it did) |
| `0006` | the `executor_spend` table: `day`, `executor` (`local`, `modal`), `kind` (`pool`, `snapshot`, `smoke`, `build`, `bench`), `usd`, `sandbox_id`, `created_at`; index `(day, executor, kind)` |

### Conventions

- String enums are `Literal` aliases stored as `VARCHAR(32)` without a CHECK constraint; values are validated in Python on bind (`db/types.py`, `str_enum`; test `test_enum_values_validated`).
- Every datetime is aware UTC (`UTCDateTime`, `timestamptz`); a naive datetime is refused on bind (`test_datetimes_normalised_to_utc_and_naive_rejected`).
- JSON columns use `MutableDict`/`MutableList`, so in-place edits of top-level keys are saved (`test_json_columns_track_in_place_changes`).
- Run ids are `run_<ULID>`: 48-bit milliseconds plus 80 random bits in Crockford base32, so they sort by creation time (`new_id`).
- Every foreign-key column leads some index, because every parent delete looks the children up by it and Postgres does not index a foreign key by itself (`test_every_foreign_key_column_leads_an_index`).

### Tables

| Table | Purpose | Key columns | Keys and indexes |
|---|---|---|---|
| `repos` | a repository under test, identified by its forge's stable id, never its name | `forge` (`github`, `fake`, `local`), `external_id`, `full_name`, `default_branch`, `private` (default true), `language`, `installation_id`, `owner_id`, `settings_json` | unique `(forge, external_id)` |
| `envs` | a dependency environment keyed by its content hash | `env_hash`, `image_id`, `python` | primary key `env_hash` |
| `snapshots` | a trusted filesystem snapshot of a default-branch commit (Modal), written by `coverset warm` and by the re-snapshot after a trusted default-branch baseline; the planner and the warm-pool maintainer read the newest `ready`, unexpired row | `repo_id`, `env_hash`, `sha`, `image_id`, `status` (`building`, `ready`, `failed`, `expired`), `expires_at` | index `(repo_id, status, created_at)`; index `env_hash` |
| `commits` | commit metadata seen in a mirror | `repo_id`, `sha`, `parent_shas`, `subject`, `author_name`, `committed_at` | unique `(repo_id, sha)` |
| `pull_requests` | a PR and its sticky comment | `repo_id`, `number`, `head_sha`, `base_sha`, `head_ref`, `base_ref`, `title`, `author`, `author_association`, `from_fork`, `draft`, `state`, `comment_id` | unique `(repo_id, number)` |
| `runs` | one CI run of a head commit | `repo_id`, `head_sha`, `base_sha`, `branch`, `pr_id`, `trigger` (`pull_request`, `push`, `baseline`, `rerun`, `manual`), `status`, `verdict`, `reason`, `trusted`, `network`, approval columns, `check_run_id`, `executor`, `env_hash`, `snapshot_id`, `shard_count`, `scoring_basis_run_id`, `coverage_complete`, `cancelled_tests`, `cost_estimate`, `rank_coverset`, `rank_file_order`, `t_first_failure_file_order_est`, `rank_executed`, `claimed_by`, `heartbeat_at`, `timeline`, `ci_first_contact_at`, `extra_env`, `retry_increment_keys`, `summary_json` | indexes `(repo_id, head_sha, status)`, `(repo_id, pr_id, created_at)`, `(repo_id, branch, created_at)`; indexes on `pr_id`, `env_hash`, `snapshot_id`, `scoring_basis_run_id` |
| `shards` | one worker's slice of a run | `status`, `worker_ref`, `test_count`, `predicted_s`, `exit_code`, `bundle_sha256`, `error`, `started_at`, `finished_at` | primary key `(run_id, shard_id)` |
| `tests` | a test node id within a repo | `nodeid`, `nodeid_hash` (16-byte blake2b), `family` (node id before the first `[`), `file_id`, `first_seen_run_id`, `quarantined` | unique `(repo_id, nodeid_hash)`; index `first_seen_run_id` |
| `test_results` | the final outcome of one test in one run | `outcome` (`passed`, `failed`, `error`, `skipped`, `xfailed`, `xpassed`, `not_run`), `failed_phase`, `fail_sig` (8 bytes), `duration_ms`, `shard_id`, `attempts`, `flaky`, `quarantined`, `score`, `score_explanation`, `rank` | primary key `(run_id, test_id)`; index `(test_id, run_id)` |
| `test_attempts` | each execution of a test (attempt 1 is the shard, attempt 2 the retry) | `outcome`, `duration_ms`, `shard_id` | primary key `(run_id, test_id, attempt)`; index `test_id` |
| `test_stats` | rolling statistics per test over its last 50 executed results | `runs_n`, `fails_n`, `last_fail_run_id`, `last_fail_at`, `last_run_at`, `fail_ewma`, `dur_ewma_ms`, `p50_ms`, `p95_ms`, `flaky_n`, `flaky_score`, `updated_run_id` | primary key `(repo_id, test_id)`; index `test_id` |
| `run_events` | the ordered, redacted event stream of a run (SSE and JSON polling) | `seq`, `kind`, `payload`, `at` | unique `(run_id, seq)` |
| `webhook_deliveries` | every received delivery | `delivery_id`, `body_sha256`, `forge`, `event`, `action`, `repo_external_id`, `status` (`received`, `processed`, `ignored`, `ignored_owner`, `error`), `run_id`, `payload` | unique `delivery_id`; unique `body_sha256`; index `received_at`; index `run_id` |
| `forge_calls` | every call made through the forge (the fake forge's outbox) | `forge`, `repo_id`, `run_id`, `method`, `target`, `request`, `response`, `status_code` | index `(repo_id, created_at)`; index `run_id` |
| `invariants` | one version of a declared invariant | `inv_key`, `spec_hash`, `spec_json`, `first_seen_sha`, `last_seen_sha` | unique `(repo_id, inv_key, spec_hash)` |
| `invariant_results` | the status of one invariant version in one run; `coverset invariants scan` rewrites the scanned baseline run's row of each review-kind invariant in place (`trigger = scan`, the run's own status kept in `evidence.scan.run_status`) | `status`, `trigger` (`run`, `scan`), `target_func_key`, `target_body_hash`, `covering_test_ids`, `evidence` | primary key `(run_id, invariant_id)`; index `(invariant_id, run_id)` |
| `review_findings` | an advisory AI-review finding for one function body, from a run's review of its changed functions or from `coverset invariants scan` (stored under the scanned baseline run) | `run_id`, `func_key`, `body_hash`, `bundle_hash`, `source` (`fixture`, `heuristic`, `anthropic`), `verdict`, `confidence`, `payload` | index `(func_key, bundle_hash)`; index `run_id` |
| `executor_spend` | spend that belongs to no run (pool idle time, trusted snapshot builds, `coverset modal smoke` sandboxes and their image builds, env image builds, benchmarks), counted with `runs.cost_estimate` against the daily budget (`orchestrator/budget.py`) | `day` (UTC), `executor`, `kind` (`pool`, `snapshot`, `smoke`, `build`, `bench`), `usd`, `sandbox_id` | index `(day, executor, kind)` |
| `settings` | control-plane key/value state: `instance_id` (the Modal executor's sandbox tag, plain text) and `github_app` (the App `/setup` stored, sealed by `db/vault.py`) | `key`, `value` (text, revision `0011`) | primary key `key` |

The `runs.timeline` keys are listed in [ARCHITECTURE.md](../ARCHITECTURE.md#how-a-run-works) and [docs/orchestrator.md](orchestrator.md#timeline).

`test_stats` is recomputed at the end of every run by `orchestrator/history.py` (`update_test_stats`). An untrusted run updates nothing. Otherwise only completed, trusted runs count, plus the run being recorded; `not_run` results never count. A test with no `test_stats` row of its own borrows only the median p50 of its `family` (the node id before the first `[`, shared by every parametrisation) for scoring; its fail rate, flakiness and run count stay empty (`load_test_history`; test `test_load_test_history_family_fallback`). A test without statistics that ran in the previous run (e.g. an untrusted run, which updates no `test_stats`) keeps that outcome and is not scored as a new test.

`run_events` and `test_results` are written in batches by `EventSink` (`orchestrator/runner.py`): every 100 ms or 200 pending rows, under the run's lock, with every payload string passed through `coverset.logging.redact`.

## The facts store

### Layout

What DuckDB reads is the facts cache, `<cache_dir>/facts/` (by default `<data_dir>/cache/facts/`); the
blob store holds the same runs as versioned objects and a `MANIFEST.json` per run
([object-store.md](object-store.md)):

```
<cache_dir>/facts/
  repo=<repo_id>/
    dictionary.json              ids for paths, node ids and shape keys (append-only)
    dictionary.lock
    run=<run_id>/
      .lock                      writers of this run serialise on it
      .sync.json, MANIFEST.json  what this cache last pulled or published
      functions.parquet          run tables: one file per run
      runs_dim.parquet
      file_cov.parquet
      func_input_cov.parquet
      ingest_log.shard=<n>.parquet                 one per shard: history and commit record
      <table>.shard=<n>.attempt=<k>.parquet        shard tables: one file per ingest attempt
```

A real run of the demo (PR #1, three shards) holds `call_shapes`, `file_arcs_possible`, `file_static`, `files`, `func_sampling`, `run_arcs`, `shapes`, `test_lines` and `tests_dim` as `.shard=0.attempt=1` to `.shard=2.attempt=1` files, three `ingest_log.shard=<n>.parquet` files, and the four run tables.

Files never store `repo_id` or `run_id`: the DuckDB views add both from the Hive partition directories. Every file is written to a hidden `.<name>.<uuid>.tmp` sibling, fsynced, then moved into place with `os.replace` (`facts/atomic.py`).

### Ids: `dictionary.json`

Paths, node ids and shape keys are stored as dense integer ids. The ids are list positions in `repo=<id>/dictionary.json`, so an id never changes once assigned and new values are only appended (`facts/dictionary.py`). Test id 0 is reserved for `<import>`, the pseudo test that holds lines executed outside any test (coverage.py's `''` context). Extensions are a compare-and-swap of the dictionary's object in the blob store, under an exclusive `flock` in the cache, so concurrent ingests (on one machine or several) never hand out the same id twice. Garbage collection never touches the dictionary. Ids are therefore stable across runs of a repo (`test_ids_are_stable_across_runs`).

### Attempts and the commit record

One shard bundle is ingested as one unit (`facts/ingest.py`, `ingest_shard`):

1. Take the run lock.
2. If the shard's latest `ingest_log` row is `ok` with the same `bundle_sha256`, stop: re-pulling a bundle is a no-op.
3. Validate the bundle's numbers against the column types, then build and type-check every table. A value that does not fit raises `BundleError` and is logged as a `failed` attempt; nothing else changes.
4. Drop the run aggregates (`file_cov`, `func_input_cov`) and mark `runs_dim` not finalised.
5. Write this attempt's shard-table files. Readers cannot see them yet.
6. Append an `ok` row to `ingest_log.shard=<n>.parquet`. **That append is the commit.**
7. Delete the files of every other attempt of the shard.

Readers see each shard's latest `ok` attempt and nothing else (`FactsStore._visible`). A crash between steps 5 and 6 leaves invisible files that the next attempt numbers past (`test_a_crash_mid_ingest_leaves_the_committed_attempt_visible`, `test_uncommitted_files_are_invisible_to_every_reader`). A bundle that cannot be read at all is logged with `record_shard_failure`: the shard's earlier rows stay visible but the shard stops counting as ingested.

Retried tests keep their first coverage. The runner never ingests retry output, and when another shard of the run already recorded a node id's lines, a later shard's lines for it are dropped (`tests_dim.has_lines = false`) while its result is still recorded (`test_a_test_rerun_in_another_shard_keeps_its_first_coverage`).

`finalize_run` computes `file_cov` and `func_input_cov` once the expected shards are in, and writes `runs_dim.coverage_complete`. Shards are numbered `0 .. expected_shards - 1`; the run is complete only when each of them has an `ok` latest attempt.

### Tables

Shard tables (one file per shard per attempt):

| Table | Columns | Notes |
|---|---|---|
| `tests_dim` | `test_id`, `nodeid`, `file_id`, `outcome`, `duration_s`, `shard_id`, `has_lines` | one row per node id of the shard, `<import>` included |
| `files` | `file_id`, `path` | the paths this shard mentions |
| `file_static` | `commit_sha`, `file_id`, `blob_sha`, `n_lines`, `statements[]`, `excluded[]`, `branch_lines[]` | from `coverage.json`; `statements` = executed ∪ missing lines; `branch_lines` = positive from-lines of possible arcs; `blob_sha` is in the schema but not filled today |
| `file_arcs_possible` | `commit_sha`, `file_id`, `from_line`, `to_line` | executed ∪ missing branch arcs from `coverage.json` |
| `test_lines` | `test_id`, `file_id`, `line` | long format, sorted by `(file_id, line, test_id)`; `test_id = 0` is `<import>` |
| `run_arcs` | `file_id`, `from_line`, `to_line` | executed branch arcs, union over the shard's tests |
| `call_shapes` | `func_key`, `param`, `shape_id`, `caller_kind` (`func` or `test`), `caller_key`, `caller_path`, `caller_qualname`, `caller_line`, `samples`, `test_ids[]`, `truncated`, `example_test_id`, `example_repr` | one row per `(function, parameter, shape, caller)`; `caller_key` is the caller's `func_key` or the calling test's node id |
| `func_sampling` | `func_key`, `path`, `qualname`, `firstlineno`, `calls`, `tests_called`, `tests_truncated`, `truncated`, `test_ids[]` | how often the tracer sampled each function; `truncated` is set by either cap (the adaptive sample cap or the full-trace safety cap); `tests_truncated` is this shard's `cap_hits` |
| `shapes` | `shape_id`, `shape_key` | the shape keys this shard used |
| `ingest_log` | `shard_id`, `attempt`, `bundle_sha256`, `status` (`ok`, `failed`), `ingested_at`, `n_tests`, `n_test_lines`, `convert_json` | one file per shard holding every attempt; the commit record. `convert_json` carries the converter's counts and, when `meta.json` names a tracer other than coverage.py's C tracer (`CTracer`), `tracer_mismatch: true` and the tracer's name; the runner then adds a `log` run event "coverage was collected with <name>; per-test contexts may be incomplete". A C tracer the plugin saw replaced mid-run (`meta.tracer_lost_at`) is recorded the same way, with the name "CTracer lost at <context>", and the event reads "coverage lost its tracer at <nodeid>; per-test contexts after it may be incomplete" |

Run tables (one file per run):

| Table | Columns | Notes |
|---|---|---|
| `functions` | `commit_sha`, `func_key`, `file_id`, `path`, `qualname`, `ordinal`, `def_line`, `body_start`, `end_line`, `body_hash`, `sig_hash`, `params_json`, `kind` (`function`, `method`, `class`, `module`), `language`, `types_json` | the head commit's regions, indexed once per run. `language` is the ingest job's (never a bundle manifest's). `types_json` is the declared types a neutral bundle shipped for a region it supplied (`{"param": descriptor or null, ..., "return": ...}`, the `types` of `functions.jsonl`), else null; the pages build a function's input classes from it, so they count what `finalize_run` stored in `func_input_cov` |
| `runs_dim` | `head_sha`, `base_sha`, `branch`, `pr_number`, `created_at`, `scoring_basis_run_id`, `expected_shards`, `ingested_shards`, `coverage_complete`, `finalized_at` | mirrored from the control database so facts SQL never needs the database |
| `file_cov` | `file_id`, `path`, `statements`, `covered`, `branches`, `branches_covered`, `partial_branches`, `percent` | `percent = 100 × (covered + branches_covered) / (statements + branches)` |
| `func_input_cov` | `func_key`, `tests_called`, `tests_sampled`, `tests_truncated`, `blocks_expected`, `blocks_observed`, `missing_json` | input-class coverage per function; the class columns stay null without a catalogue. `tests_truncated` is counted over the whole run with `analysis/shapes.cap_hits`, the definition the verdict and the UI use: 0 when no shard capped the function, else the tests that called it (lines in its body) but were never sampled, at least 1 |

Per-run summaries of `test_lines` (run tables, written by `finalize_run` through `facts/aggregates.py`, dropped with `file_cov` whenever a shard is ingested again):

| Table | Columns | Notes |
|---|---|---|
| `func_body` | `func_key`, `statements`, `covered`, `lines[]`, `covered_lines[]` | each callable's body by the function page's rule (statements in `[body_start, end_line]` minus excluded; a line ran when a test ran it, or `<import>` did and it is not the def line); what the overview's gaps, a function's history and the class changes read instead of scanning `test_lines` |
| `tree_leaf` | `file_id`, `func_key`, `statements`, `covered` | the treemap's leaves (the innermost callable owns a statement, else the file's module leaf, `func_key` null) |
| `line_hits` | `file_id`, `line`, `tests`, `by_import` | per executed line: the tests other than `<import>` that ran it and whether `<import>` did; the source view's line states and per-line test counts |

A reader uses a summary only when it is newer than every file it was computed from (`queries.summary_is_fresh`: one listing and a few `stat` calls), else it computes the same answer from `test_lines`, so runs finalized before the summaries existed, and facts edited by hand, read correctly (`test_summaries_answer_exactly_what_test_lines_answers`, `test_a_summary_older_than_its_sources_is_ignored`). `line_hits.tests` is a `count(*)`: a run stores each (test, file, line) cell once (ingest drops repeated cells, and a test's lines live in one shard).

The compact layout (shard table, written only with `COVERSET_FACTS_COMPACT=1`, see [Compact layout](#compact-layout-test_file_bits)):

| Table | Columns | Notes |
|---|---|---|
| `test_file_bits` | `test_id`, `file_id`, `n_lines`, `bits` | one row per (test, file): the lines as a bitset in coverage.py's numbits format (line `n` is bit `n % 8` of byte `n // 8`), sorted by `(test_id, file_id)`; `n_lines` is the bit count |

Line numbers are `int32`. coverage.py arcs use negative numbers for code-object entry and exit, so arc columns allow them.

**Schema changes.** Facts files carry no version number: `facts/schema.py` is the one current schema, and `FactsStore.read_table` conforms every file to it on read (a column a file lacks reads as nulls) while the DuckDB views read with `union_by_name`. A new nullable column therefore needs no migration. `functions.types_json` was added this way (2026-10-11): runs ingested before it read null there, and their pages fall back to parsing the head source, as before.

### Function regions and `func_key`

`analysis/functions.py` indexes every `def`, `async def` and `class` reachable through `ast.iter_child_nodes`, so definitions nested in `if`, `try`, `with` and `match` blocks are found. Each region has:

| Field | Rule |
|---|---|
| `qualname` | CPython's `co_qualname` convention: `outer.<locals>.inner`, `Cls.method` |
| `def_line` | the first decorator line, else the `def` line; equal to `co_firstlineno` |
| `body_start` | the first body statement after a leading docstring; `def_line` when the body is only a docstring or the def is one line |
| `end_line` | `end_lineno` of the node |
| `ordinal` | index among same-qualname definitions in the file (`@overload` stubs, property getter and setter, `try`/`except` redefinitions) |
| `func_key` | `blake2b(path \0 qualname \0 ordinal, digest_size=8)` as 16 hex digits |
| `body_hash` | blake2b-8 of the AST dump of the body without its docstring; formatting- and comment-insensitive. Class and module bodies replace nested definitions with `pass`, so editing a method changes the method's hash only. |
| `sig_hash` | blake2b-8 over arguments, decorators, return annotation and type parameters |

One extra region of kind `module` and qualname `<module>` stands for module-level code. Shape observations are joined to regions by the function triple `(path, qualname, firstlineno)`; when a triple has no exact match, a unique `(path, qualname)` match is used, else the observation keeps the key of ordinal 0 and is counted as unmatched (`test_unmatched_functions_fall_back_to_ordinal_zero`).

## The coverage rules

### The body-line rule

**A function counts as tested only when a line of its body ran inside a test.** "Test T calls function F" means T's executed lines intersect `[body_start, end_line]`. Import time runs every `def` and decorator line, so those lines say nothing about whether a test called the function.

The rule drives four things: the changed-function table, covering-test lists, `impacted_by_diff` in scoring, and wave 1. `<import>` lines count towards the run's union for patch coverage, but never make a test "call" a function and never appear in covering-test lists or ordering.

`analysis/patch.py` applies the rule to changed signatures: a changed decorator or `def` line counts as covered only when some body line of that function ran. When `body_start == def_line` (a one-line def, or a docstring-only body), that line counts as covered only when a real test, not `<import>`, executed it (`test_decorator_only_change_is_not_covered_by_import`, `test_docstring_only_and_one_line_defs_need_a_test_call`).

### Statement first lines

coverage.py's tracer reports the physical lines it saw, but `coverage.json` lists statements by their first line. A multi-line call reports its continuation lines too. `facts/convert.py` (`first_lines`) maps every raw executed line to the first line of the statement that contains it, which reproduces coverage.py's own `translate_lines` without re-parsing the source. A raw line that is not a statement start belongs to the nearest statement start above it; lines inside excluded statements keep their number. The converter checks this against `coverage.json` on parenthesised and multi-line expressions, comprehensions, stacked decorators, `try`/`else`/`finally`, `with`, `match`, `while`/`else`, lambdas and excluded code (`test_per_test_lines_are_statement_first_lines`, `test_first_lines_maps_continuations_and_keeps_excluded_lines`).

### Patch coverage

```
patch coverage = |changed ∩ statements ∩ executed| / |changed ∩ statements|
```

`changed` is the head-side added lines of `git diff base...head`; `statements` excludes excluded lines; `executed` is the run's union over all tests, `<import>` included. Files without static facts (docs, unmeasured paths) are left out and listed as unmeasured.

There are two implementations with one difference. `facts/queries.py` (`patch_coverage`) is plain SQL over the run's views; while shards are missing it reports uncovered changed lines as `unknown` instead of `missing`, and the percentage is then a lower bound (`test_patch_gaps_are_unknown_while_shards_are_missing`). `analysis/patch.py` (`patch_report`) is what the verdict uses: it adds the signature-line rule above and builds the changed-function rows (tests, body line percentage, inherited flag for `trivial`, `renamed` and `moved` changes).

A branch is partial when its from-line ran and not all of its possible exits are in `run_arcs` (`test_partial_branches`).

### Querying with DuckDB

There is no DuckDB database file. A connection is an in-memory database with views over the committed Parquet files (`FactsStore.connect`):

1. `duckdb.connect` with `autoinstall_known_extensions` and `autoload_known_extensions` off.
2. `SET allowed_directories` to the directories in scope (the facts root only for a connection that spans every repo; the repo's directory, or only its runs' directories, for a scoped one, so a scoped connection and the user SQL run on it can never `read_parquet` another repository's files; `test_a_scoped_connection_cannot_read_another_repository`) and `SET enable_external_access = false`.
3. `SET memory_limit` and `SET threads` from `COVERSET_FACTS_MEMORY_LIMIT` (default `512MB`) and `COVERSET_FACTS_THREADS` (default `min(4, CPUs)`).

A cache or thread knob whose value is not a positive integer (`0`, `-1`, `abc`) is ignored and the default applies; coverset does not clamp it.
4. One view per requested table: `read_parquet([<explicit list of committed files>], hive_partitioning=true, union_by_name=true, hive_types={'repo': BIGINT, 'run': VARCHAR})`, with `repo_id` and `run_id` taken from the partition directories. A table with no files gets an empty view of the right schema.
5. `SET lock_configuration = true`.

Views can be scoped to one repo, one run, or a list of runs, and to the tables a query needs. Readers do not build them per query: `FactsStore.lease` hands out a cursor of a cached connection for the scope (see [Caches](#caches)), which every function of `facts/queries.py` uses. Because nothing holds a lock on a database file, the CLI, scripts, tests and screenshots can query while `coverset serve` ingests. A query that hits a file replaced mid-read is retried once, and the retry's lease sees the new files.

User SQL goes through `FactsStore.query`, which accepts exactly one `SELECT` statement (`test_query_accepts_only_a_single_select`, `test_connection_cannot_reach_other_files`). From the CLI:

```sh
uv run coverset facts query "SELECT path, statements, covered, branches, branches_covered, round(percent, 1) AS pct \
  FROM file_cov WHERE path LIKE '%allocate%'" --repo-id 1 --run <run_id>
```

| Question | Function in `facts/queries.py` | Approach |
|---|---|---|
| Patch coverage of a diff | `patch_coverage` | the changed `(path, line)` pairs are registered as an Arrow table and joined to `file_static` and the distinct `test_lines` |
| Tests covering a function | `tests_covering(path, start, end)` | distinct `test_id` in `test_lines` for the file with `line BETWEEN start AND end`, `<import>` excluded by default, joined to `tests_dim` for node ids |
| Tests covering one line (the source-view popover) | `line_tests` | `tests_covering` with `start = end`, `<import>` included |
| Treemap aggregates | `treemap` | each statement belongs to the innermost function whose body contains it, else to the file's `<module>` leaf; leaves roll up to files and directories; `input_cov` from `func_input_cov` |
| Shape history | `shapes_for_function(func_key, last_n_runs=5, branch=None)` | pick the newest runs from `runs_dim` first (only runs whose `runs_dim.branch` is `branch` when given; `coverset shapes` passes the default branch unless `--branch any`), then read only those runs' `call_shapes`, grouped by `(param, shape_id)`: summed samples, run count, test ids, and the example that sorts first by (repr length, repr, test node id), so the choice does not depend on run or test order |
| Run headline | `run_summary` | `runs_dim`, outcome counts from `tests_dim` (a test seen by several shards takes the most recently committed one), totals from `file_cov` |
| Prior-run facts for scoring and wave 1 | `ParquetPriorRun` | loads `functions`, `test_lines`, `file_static` and `tests_dim` of one run into sorted numpy arrays |
| What one test ran (the test page, `coverset facts test`) | `test_touched(nodeid)` | the test's ids from `tests_dim`, its `test_lines` joined to `files` and `file_static`; each line goes to the innermost callable in `functions` whose body contains it (the treemap's rule), else it is module-level; per file the lines and statement count, per function (by `def_line`) the body lines run and the body's statements |
| What one test passed in | `test_shapes(nodeid)` | `call_shapes` rows whose `test_ids` hold the test (or whose caller is the test), with `shapes` for the key and the region triple from `functions` (else `func_sampling`); returned as `ShapeObs`, whose `samples` count every test of the row |
| One test's outcomes over recent runs | `test_history(nodeid, last_n)` | the newest runs from `runs_dim` first, then their `tests_dim` (the most recently committed shard wins) left-joined, newest first; first-attempt outcomes (`test_results` in the control database has the final outcome and the flaky flag) |
| Inventory of a run (`coverset facts tables`) | `run_inventory` | one `count(*)` per view in one `UNION ALL`, plus the sizes of the visible (committed) files per table |
| Ingest history of a run (`coverset facts explain`) | `ingest_record` | every `ingest_log` row by shard and attempt, the committed attempt marked, `convert_json` parsed into tracer notes, dropped records and bundle bytes |
| Default-branch trend (the repo overview, the dashboard) | `baseline_trend(branch, last_n=12, candidates=None)` | the newest finalized `runs_dim` runs of `branch` with no PR number first (the web passes the newest no-PR runs of the branch from the database's `runs` as `candidates`, so only their `runs_dim` is read, not every run's; an empty list means none, and a list that yields no baseline falls back to scanning every run), then per run, oldest first: `file_cov` totals (statements, lines run, branches taken and possible), callables in `functions` left-joined to `func_input_cov` (functions called by a test, total, classes expected and observed) and distinct non-`<import>` test ids in `tests_dim` |
| One function over the baselines (the function page's history) | `function_history(func_key, branch, last_n=12, until=None)` | the same baselines (created at or before `until`: the page anchors the window at the run it shows); per run the region (absent runs are points with `present` false), its `func_input_cov` row (expected, observed, tests called, sample-cap hits, the missing class names) and the body's line % by the function page's rule: statements in `[body_start, end_line]` minus excluded, a line counting when a test ran it or `<import>` did and it is not the def line |
| What changed between two runs | `class_changes(run_id, prior_run_id)` | functions of both runs matched by `func_key`, else by `(path, qualname)` (lowest prior ordinal); classes missing now and not then are "lost", the reverse "gained" (named only when the signature digest `sig_hash` and the expected class count are the same in both runs, else `uncompared`; only missing classes are stored, so this is inferred, and even at the same count the expected set can differ, one class becoming expected as another stops, which is why the pages say "missing now, not missing then"; `sampled` when this run hit the sample cap), and for an unchanged `body_hash` the body statements that ran then and not now, matched by offset from `body_start`. Only functions where something changed |
| Largest gaps of a run | `top_gaps(limit=10)` | called functions (`func_input_cov.tests_called > 0`) by expected minus observed classes, then body size, path and name; with the body's line % and the missing class names from `missing_json` |

As an example, on the PR #2 run of a local demo, `SELECT count(DISTINCT tl.test_id) FROM functions f JOIN test_lines tl ON tl.file_id = f.file_id AND tl.line BETWEEN f.body_start AND f.end_line WHERE f.qualname = 'validate_email' AND tl.test_id <> 0` returned 24, and `file_cov` showed `src/ledger/accounts/emailcheck.py` and `src/ledger/billing/allocate.py` at 100.0% line and branch coverage.

### Caches

Every file of the store is immutable once visible: a run changes only by new files (an attempt, its commit row in `ingest_log`, a finalize's run tables), an atomic replace (a new inode under the same name) or a deletion (superseded attempts, gc). So a run's **stamp**, the sorted `(name, inode, size, mtime_ns)` of its `*.parquet` entries (`facts/cache.py`, `run_stamp`: one `scandir` and one `stat` per file, 36 µs for a finalized run), changes whenever anything a reader could see changes. Every cache below is keyed by the stamps of the runs it read and recomputes them on every lookup, so there is no invalidation message to miss: an ingest, a finalize or a gc makes the very next lookup miss. The inode is in the stamp because a replace within one tick of a coarse filesystem clock (ext4) keeps the mtime and often the size.

| Cache | Key | Holds | Bound | Code |
|---|---|---|---|---|
| connections | store root, scope (repo, runs), tables, the scope's allowed directories + the scope's stamps | one in-memory DuckDB database per scope with its views; `FactsStore.lease` hands out a cursor of it | `COVERSET_FACTS_CACHE_ENTRIES` (16) databases, each with `COVERSET_FACTS_MEMORY_LIMIT` (512MB) and `COVERSET_FACTS_THREADS` | `facts/cache.py` `ConnectionCache`, `facts/store.py` `lease` |
| query results | query, arguments, runs + their stamps | the result of `treemap`, `top_gaps`, `class_changes`, `run_summary`, `test_touched`, `test_shapes`, the picked baselines and the points of `baseline_trend` and `function_history` | `COVERSET_FACTS_RESULT_ENTRIES` (256) | `facts/cache.py` `ResultCache`, `facts/queries.py` `_memo` |
| run facts | store root, repo, run + its stamp | the web's `IndexedRunFacts`: `test_lines` as sorted arrays with an index of each file's rows, `line_hits` by file, `call_shapes` sorted by function with each function's row span and `func_sampling` rows by function, and the memoised function views, line facts, body coverage and branch gaps | `COVERSET_RUN_FACTS_ENTRIES` (8) runs and `COVERSET_RUN_FACTS_MB` (384) of estimated bytes: `memory_bytes` weighs everything an entry holds (arrays, Arrow tables, the Python graphs of its views and memo entries, by sampling; 86 MB for a fully warmed run of the real bundle against 77 MB from an exact walk), checked on every lookup, hits included; the newest run always stays | `web/loaders.py` `cached_run_facts` |
| source text | mirror, commit, path | blob text; the first miss for a commit reads every measured source of the run in one `git cat-file --batch` (if that batch fails, for example on a missing path with a space that `read_blobs` cannot parse, the requested path is read alone and the commit's later misses read one path each) | `COVERSET_SOURCE_CACHE_MB` (64) | `web/loaders.py` `read_source` |
| highlighted source | path, text | the Pygments lexing of a file (`render/source.py` `SourceCache`) | 32 texts | `web/loaders.py` `source_cache` |

Details that matter for correctness:

* A cursor (`DuckDBPyConnection.cursor()`) is its own connection to the cached database: it sees the views, it cannot change the locked configuration, and an Arrow table registered on it (`patch_coverage`'s changed lines) is visible to it alone (`test_an_arrow_table_registered_on_a_lease_is_private_to_it`). Empty tables are real (empty) tables, not registered Arrow objects, for that reason.
* An evicted or superseded database is never closed: closing a DuckDB connection closes its cursors, which another thread may be using. It is dropped and freed with its last cursor (`test_a_cursor_outlives_the_eviction_of_its_connection`).
* There is one database per scope, never one shared database with a schema per scope, so a user query on one run's scope cannot name another scope's views (and `allowed_directories` keeps it inside the repo). The allowed directories are part of the key: two scopes that list the same runs, such as two repos with no runs yet or the unscoped and repo scope of a one-repo store, never share a database (`test_scopes_with_the_same_runs_never_share_a_connection`).
* The run-facts budget bounds what the cache holds, not the process: freed run facts leave about 450 MB of allocator fragmentation on the real bundle (measured with `scripts/bench_facts.py --cache-memory`; `docs/benchmarks.md`, "Memory", has the footprint per budget and the sizing advice).
* Memoised list results are returned as copies, and every memoised value is a frozen dataclass, so a caller's sort or append never reaches the cache.
* `IndexedRunFacts` answers exactly what `RunFacts` answers (`test_indexed_run_facts_answer_what_run_facts_answers` compares every method on a finalized and an unfinalized run); `pr_view.analyse_diff` keys its own cache by the facts object, so it follows the same stamps.

The static files are cached by the browser instead: `url_for('static', ...)` carries `?v=<12 hex digits of the file's SHA-256>`, a request at the current version is `Cache-Control: public, max-age=31536000, immutable`, any other revalidates (`no-cache` with the ETag), and HTML, CSS, JavaScript, SVG and JSON responses over 1 KiB are gzipped for clients that accept it (level 3 for pages: the 3 MB treemap page goes out as 267 KB for 6 ms of CPU; static files once at level 9, kept). Streamed responses (the SSE event stream) are never compressed (`app.py` `install_response_caching`, `tests/web/test_static_caching.py`).

Gzip and BREACH: every page carries the session's fixed CSRF token (`<body hx-headers>`) and many reflect query input. With compression, a network observer who can make the victim's browser request a page with chosen input could recover the token from response lengths. So a dynamic response is compressed only when the browser marks the request `Sec-Fetch-Site: same-origin` or `none`: the app's own links and htmx requests, a typed URL or a bookmark. An attacker's page cannot issue such a request. A request started by another site (`cross-site`, `same-site`) gets the identity encoding, and every dynamic response varies on `Sec-Fetch-Site`. A client that sends no `Sec-Fetch-Site` (curl, the CLI, an old browser) gets gzip only when the body does not contain its session's CSRF token. Static files hold no secret and are always compressed (`test_dynamic_bodies_are_never_compressed_for_another_site`). Masking the token per response (sending a random pad and the token XOR the pad) would allow compressing everything, but it belongs to `web/auth.py`, so it is left as a follow-up.

### Compact layout (`test_file_bits`)

`test_lines` is one row per (test, file, line). Sorted by `(file_id, line, test_id)` and written with zstd and dictionary encoding it is already small (2.2 MB for the real 5,099-test bundle's 2.1 million rows, 5.9 MB for the synthetic 20 million), but every question about one test scans all of it, and it is most of a run's bytes. The compact layout stores the same cells as one bitset per (test, file) in coverage.py's numbits format (`facts/convert.py` `lines_to_bitsets`, built with no Python loop; `bitsets_to_lines` unpacks a whole table at once), as the shard table `test_file_bits`, **next to** `test_lines` when `COVERSET_FACTS_COMPACT=1`. Readers use it where every committed shard of the run has it (`queries.has_compact_layout`); today that is `test_touched` (the test page and `coverset facts test`), which decodes only the test's rows instead of filtering every row.

Measured (`docs/benchmarks.md`, "Compact layout"):

| | real bundle (2.1M cells, 115,572 test-file pairs) | synthetic (20M cells, 202,000 pairs) |
|---|---|---|
| `test_lines` Parquet | 2.22 MB | 5.86 MB |
| `test_file_bits` Parquet (by test, file) | 0.33 MB (6.7x smaller) | 1.24 MB (4.7x smaller) |
| build at ingest | 0.17 s | 0.39 s per 2.5M-cell shard |
| one test's lines (p50) | 3.4 ms long, 1.5 ms bits | 6.3 ms long, 2.8 ms bits |
| one line's tests (p50) | 2.3 ms long, 1.6 ms bits (file-sorted copy) | 6.3 ms long, 2.6 ms bits |
| all cells back to arrays | 0.04 s | 0.11 s |

**Recommendation.** Keep `test_lines` as the source of truth in v1 and leave the flag off by default: with the summaries above, the pages no longer scan `test_lines` per request, and writing both layouts costs 8 to 11 % more bytes per run for one faster query. Turn it on where the test page's facts query matters (2.3x faster on the 20-million-cell run); it is safe to switch either way at any time (a run with bitsets in only some shards reads `test_lines`). The bitsets become the default when the remaining `test_lines` readers move over, and then `test_lines` can stop being written:

1. `finalize_run` computes `file_cov`, `func_input_cov`'s callers and the three summaries from the in-memory `_LineIndex` arrays of the ingest (or from `bitsets_to_lines`) instead of SQL over `test_lines`.
2. `patch_coverage` and `function_history`'s fallback read `line_hits` (union of executed lines) and `func_body`, which already cover them for finalized runs.
3. `tests_covering` / the line popover and `ParquetPriorRun` decode the bitsets of the files they touch (sorting `test_file_bits` by `(file_id, test_id)` instead makes that read 0.46 MB on the synthetic run and keeps per-test reads at a row-group skip).
4. Ingest writes `test_file_bits` only; a run keeps reading `test_lines` wherever it has them, so old runs need no rewrite and age out with retention.

That takes a 5,000-test run from 4.3 MB to about 2.4 MB (see [Measured ceiling](#measured-ceiling)).

### Debugging a run

Five `coverset facts` subcommands answer the usual "what did this run record?" questions without
SQL. Each takes `--repo` (id, full name or path; default the only repository), `--run` (default
the newest completed default-branch baseline, as on the UI pages; required for `explain` and
`export`) and `--json` for machine output (`commands/facts.py`; tests
`tests/orchestrator/test_commands_facts.py`).

| Command | Shows |
|---|---|
| `uv run coverset facts tables --repo coverset-demo/ledger --run <run_id>` | every facts table of the run: kind (run or shard), visible files, rows, bytes |
| `uv run coverset facts explain --repo coverset-demo/ledger --run <run_id>` | the run row, the timeline in seconds, shards, the ingest log per attempt with tracer notes and dropped records, `coverage_complete`, sample caps hit, bundle sizes and the run it was scored against |
| `uv run coverset facts test tests/test_refund.py::test_refund_partial --run <run_id>` | the result, the files and functions the test ran with their body lines, what it passed in per parameter, its outcomes in the last runs |
| `uv run coverset facts line src/ledger/billing/refund.py:12 --run <run_id>` | the function the line belongs to and the tests that ran it, in run order (rank), with outcomes |
| `uv run coverset facts export --repo coverset-demo/ledger --run <run_id> --to /tmp/run-facts` | every table as `<table>.jsonl`, a `tables.json` index (rows, columns, source files) and the repo's `dictionary.json`; facts hold no secrets (example values are redacted by the tracer and failure text is not in facts). `--to` must be new, empty or a previous export (it carries a `.coverset-export` marker); `--force` overrides that, and files are always created fresh, never through a symlink |

## Retention and gc

| Setting | Default | What it governs |
|---|---|---|
| `COVERSET_RETENTION_RUNS_DAYS` | 30 | runs, their rows and their facts directories |
| `COVERSET_RETENTION_EVENTS_DAYS` | 7 | `run_events` rows |
| `COVERSET_RETENTION_DELIVERIES_DAYS` | 14 | webhook payloads are nulled after this; the rows stay at least 30 days so redeliveries are still deduplicated |

`coverset gc [--dry-run]` (`commands/maintenance.py`) deletes runs older than the cutoff that are not queued or running, deletes old `run_events`, nulls old delivery payloads, deletes delivery rows past `max(30, COVERSET_RETENTION_DELIVERIES_DAYS)` days, and removes expired facts directories with `facts/gc.py` (`expire_runs`). It always keeps the latest completed baseline per repo and the latest run per open PR. A facts run's age is its `runs_dim.created_at`, else the directory's mtime. A run directory is renamed to a hidden name before deletion, so readers never see half a run.

`coverset repo purge NAME` deletes a repository's rows (its scoped settings included), its facts directory, its mirror, its uploaded sources and its runs' upload archives.

Production windows imported with `coverset flows import` live beside the runs, under `repo=<id>/prod/service=<service>/batch=<stamp>-<sha12>/` (tables `prod_windows`, `prod_funcs`, `prod_shapes`, `prod_calls`, `prod_flows`; `PROD_SCHEMAS` in `facts/schema.py`, kept out of `SCHEMAS`), never in a run directory, so the run views and `coverset gc` never see them. Their shape ids come from the same `dictionary.json` as `call_shapes`. Their retention is `coverset flows prune --older-than-days N` (default 30); `repo purge` removes them with the repo. See [production-flows.md](production-flows.md#data-model).

The same pass (`orchestrator/history.py`, `collect_garbage`) runs in-process: `serve` starts it as a Quart background task when it starts serving and then every 24 hours (`app.py`, `schedule_gc`; stopped by the app's shutdown event). After each PR run the runner applies the per-PR rule (`history.expire_pr_runs`): the PR's latest run stays, its other runs older than `COVERSET_RETENTION_RUNS_DAYS` are deleted with their facts. Both take a module-level lock without waiting, so a second gc in the same process skips instead of racing, and neither runs under `COVERSET_ENV=test` unless a test invokes it.

## The NeutralBundle contract

`facts/ingest.py` consumes only `NeutralBundle`, never a pytest-specific file. The dataclasses live
in the dependency-free `coverset_model` package (`packages/coverset-model`,
`coverset_model.neutral`) and `coverset.lang.base` re-exports them. One per shard:

| Field | Type | Meaning |
|---|---|---|
| `meta` | `dict` | free-form; `meta["convert"]["bundle_sha256"]` is the bundle's identity when present; a neutral bundle's reader adds `meta["bundle"]` (format, version, language, producer, created_at, shard) |
| `collected` | `tuple[CollectedItem, ...]` | `nodeid`, `file`, `line`, `markers`, `invariant_ids`, `hypothesis` |
| `tests` | `tuple[TestRow, ...]` | `nodeid`, `file`, `outcome`, `duration_s`, `failed_phase`, `fail_sig`, `longrepr`, `start`, `stop` |
| `coverage` | `dict[str, dict[str, tuple[int, ...]]]` | test id → path → executed statement first lines; the key `<import>` holds lines run outside tests |
| `static` | `dict[str, StaticFile]` | `path`, `n_lines`, `statements`, `excluded`, `arcs_possible` |
| `run_arcs` | `dict[str, tuple[tuple[int, int], ...]]` | path → executed branch arcs, union over tests |
| `shapes` | `tuple[ShapeObs, ...]` | `func` triple, `param`, `shape_key`, `caller` (a triple or a test id), `samples`, `tests`, `truncated`, `example`, `example_test` |
| `sampling` | `tuple[FuncSampling, ...]` | `func` triple, `calls`, `tests`, `truncated`, `cap` (`"sample"`, `"full"` or None) |
| `durations` | `dict[str, float]` | seconds by test id |
| `functions` | `tuple[FunctionRegion, ...]` | function regions the bundle ships (`functions.jsonl`); empty for a converted pytest-coverset bundle |
| `types` | `Mapping[FuncTriple, Mapping[str, TypeDescriptor \| None]]` | declared type of each parameter and `return` per function triple (a read-only `FrozenMap`) |

Paths are git-root-relative POSIX. Test ids may be any string up to 512 characters. Shape keys
must follow the grammar in [docs/neutral-bundle.md](neutral-bundle.md#shape-keys).

**The on-disk format is built.** [docs/neutral-bundle.md](neutral-bundle.md) specifies it
(`manifest.json`, `tests.jsonl`, `files.jsonl`, `coverage.jsonl`, `arcs.jsonl`, `functions.jsonl`,
`shapes.jsonl`, `links.jsonl`, each `.jsonl` optionally gzipped, as a directory or a flat
`tar.gz`), [docs/schema/](schema/) holds its JSON Schemas, and `coverset_model.io` reads, validates
and writes it. A worker's bundle is read by `coverset.lang.read_any_bundle`: a `manifest.json` that
says `coverset-neutral` selects the neutral reader for any language, anything else is a
pytest-coverset bundle for `facts/convert.read_python_bundle`. Both readers take the same
`BundleLimits` (a `coverset_model.io.Limits` with the Python reader's extra caps) and the head tree as the path allow-list. The neutral reader refuses a
bundle over the size, gzip-ratio, member, distinct-id, path, shape-key, function or cell limits,
an unknown version, a missing required file, a file with more lines than its row cap or a read past
its wall-clock budget (a `BundleError`, so the shard is recorded as failed
and the run's coverage is partial, as for Python); it drops and counts every row that breaks the
schema or the rules the schema cannot say (unsafe paths, repeated ids, lines that are not
statements, function keys and line ranges, shape keys outside the grammar) under
`meta["convert"]["dropped"]`, which the ingest log keeps. `coverset bundle validate`, `info` and
`convert` expose the same checks on the command line.

**Function regions.** The run's `functions` table (the body-line rule, `func_key`, the treemap,
input-class coverage per function) needs regions for every measured file. The language adapter
indexes the head commit's sources where it can (`PythonAdapter.index_functions`, the planner's
`_index_head`); for every path it did not index (another language, or a file the adapter cannot
parse) the ingest of a non-Python job takes the regions the bundle shipped in `functions.jsonl`,
with their declared types (`orchestrator/bundles.job_regions` through `lang/neutral.merge_regions`:
per path, the adapter's regions win). A Python job takes nothing language-specific from a bundle:
its regions are the planner's index alone and its `language` is `python`, whatever the manifest says. `NeutralAdapter`, built from a bundle, serves those shipped regions to anything that
asks it (`index_functions`, `changed_functions` by comparing the base and head bundles' regions and
hashes), and its `shape_catalogue` reads the shipped type descriptors, so the static facts
(`files.jsonl`), the regions and the declared types of a TypeScript run all come from its bundle.

## Measured ceiling

`scripts/bench_facts.py` generates synthetic shard bundles at the plan's scale (2,000 files, 200,000 statement lines, 10,000 tests × 2,000 lines per test in 8 shards, 30 runs, 1,000,000 `call_shapes` rows) and times conversion, ingest and finalisation, plus the p50 and p95 latency of `line_tests`, a 500-line `patch_coverage`, `treemap` and `shapes_for_function`; it also packs every shard as the archives a worker uploads (the pytest-coverset `bundle.tar.gz` and the same bundle in the neutral format) and pushes each set through the control plane's own path (extract, `read_any_bundle` under the strict limits, `ingest_shard`, `finalize_run`) in a fresh process, reporting its peak RSS. `--out docs/numbers.json` merges its `facts_*` keys into that file. With `--real DIR --repo-root GIT` it ingests a real `pytest --coverset` output into a scratch control plane and times every page instead. The figures are in [docs/benchmarks.md](benchmarks.md).

What one run costs on disk, measured (bytes of the visible Parquet files of a finalized run; summaries and the test-economy tables included):

| Repository | `test_lines` rows | `test_lines` | whole run | runs per 10 GB of facts |
|---|---:|---:|---:|---:|
| real, 5,099 tests, 464 measured files, 1 shard | 2,077,269 | 2.2 MB | 4.3 MB | about 2,300 |
| real, with the compact layout instead of `test_lines` (projected) | | 0.33 MB | 2.4 MB | about 4,100 |
| synthetic, 10,000 tests × 2,000 lines, 8 shards | 20,160,000 | 5.9 MB | 11.3 MB | about 880 |

The control database adds about 1 MB per run of a 5,000-test repository (one `test_results` row per test; the `tests` and `test_stats` rows are per repository), plus `run_events` rows, which expire after `COVERSET_RETENTION_EVENTS_DAYS`. At the default 30-day run retention a repository that runs 5,000 tests 60 times a day (pull requests and baselines) keeps 1,800 runs: 7.7 GB of facts, under one 10 GB volume.

## Scale-out path

When one machine's Parquet directory stops being enough, three steps apply in order, each behind the same `FactsStore` and `facts/queries.py` functions. The first is half built: the per-(test, file) bitsets exist (`test_file_bits`, [Compact layout](#compact-layout-test_file_bits)), measured at 6.7x smaller than `test_lines` on the real bundle; what is left of step 1 is keying them by content.

1. **Content-addressed per-(test, blob) bitsets.** Most tests execute the same lines of an unchanged file run after run. Key each `test_file_bits` row by `(test, file blob sha)` instead of by run and reference it from each run, so an unchanged file costs one reference per run instead of one bitset (and the long table's one row per line). On the real bundle a run's bitsets are 0.33 MB; with most files unchanged between baselines, a run would add tens of kilobytes.
2. **DuckLake.** Keep the Parquet files, move the catalogue (which files belong to which run and attempt, today `ingest_log`) into a DuckLake catalogue, so several control planes can read and write the same facts.
3. **Postgres with `pg_roaringbitmap`.** Store the per-(test, blob) bitsets as roaring bitmaps in Postgres, which makes "tests covering line L" and patch coverage set operations inside the database.

Before any of these, one process serves more than this page's ceiling suggests: the pages read per-run summaries and memoised results (see [Caches](#caches)), so a request costs O(functions of the page), not O(cells of the run); what grows with the number of runs is disk (above) and, at ingest, memory. Converting and ingesting a shard runs in the ingest pool's worker processes (the default `process` mode), so its peak stays out of the server; `finalize_run` runs in the runner, inside the server process, and on a 20-million-cell run it peaks at 1 GB without the test-economy pass and 3 GB with it (measured in `docs/benchmarks.md`). Running `finalize_run` in the ingest pool as well is the next step for a server with little memory.
