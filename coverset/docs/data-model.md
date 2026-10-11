# Data model

coverset keeps two stores with different shapes.

| Store | Holds | Size per run | Technology | Code |
|---|---|---|---|---|
| Control-plane database | repositories, PRs, runs, tests, results, events, deliveries, invariants, findings | O(files) + O(tests) rows | SQLAlchemy 2.1 async, SQLite in WAL mode | `src/coverset/db/` |
| Facts store | per-test executed lines, branch arcs, function regions, argument shapes, run aggregates | O(tests × lines) rows | immutable Parquet files (zstd, dictionary encoding), queried by in-memory DuckDB | `src/coverset/facts/` |

Per-line coverage and argument shapes never go into SQL. The SQL store stays small enough for SQLite; the Parquet store is written once per shard and never updated in place, so any number of processes can read it while one writes.

## Control-plane database

### Connection and migrations

Every SQLite connection gets `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000` and `foreign_keys=ON` (`db/session.py`, `SQLITE_PRAGMAS`; test `test_sqlite_pragmas`). The default URL is `sqlite+aiosqlite:///<data_dir>/coverset.db`. `COVERSET_DATABASE_URL` accepts any SQLAlchemy async URL, but only SQLite is exercised.

Migrations are Alembic revisions under `src/coverset/db/migrations/versions/`, run with `render_as_batch=True` so SQLite can alter tables. `coverset init-db` migrates explicitly, and the app migrates when it starts serving unless `COVERSET_SKIP_MIGRATIONS=true`. Test `test_migrations_match_models` checks that the revisions produce exactly the models.

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
- Every datetime is aware UTC (`UTCDateTime`); SQLite stores it naive and gets `tzinfo=UTC` back on load; a naive datetime is refused on bind (`test_datetimes_normalised_to_utc_and_naive_rejected`).
- JSON columns use `MutableDict`/`MutableList`, so in-place edits of top-level keys are saved (`test_json_columns_track_in_place_changes`).
- Run ids are `run_<ULID>`: 48-bit milliseconds plus 80 random bits in Crockford base32, so they sort by creation time (`new_id`).
- Every foreign-key column leads some index, because SQLite looks children up by it on every parent delete (`test_every_foreign_key_column_leads_an_index`).

### Tables

| Table | Purpose | Key columns | Keys and indexes |
|---|---|---|---|
| `repos` | a repository under test, identified by its forge's stable id, never its name | `forge` (`github`, `fake`, `local`), `external_id`, `full_name`, `default_branch`, `private` (default true), `language`, `installation_id`, `owner_id`, `ai_review_enabled`, `settings_json` | unique `(forge, external_id)` |
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
| `settings` | control-plane key/value state | `key`, `value` (2,048 characters, sized for secrets) | primary key `key` |

The `runs.timeline` keys are listed in [ARCHITECTURE.md](../ARCHITECTURE.md#how-a-run-works) and [docs/orchestrator.md](orchestrator.md#timeline).

`test_stats` is recomputed at the end of every run by `orchestrator/history.py` (`update_test_stats`). An untrusted run updates nothing. Otherwise only completed, trusted runs count, plus the run being recorded; `not_run` results never count. A test with no `test_stats` row of its own borrows only the median p50 of its `family` (the node id before the first `[`, shared by every parametrisation) for scoring; its fail rate, flakiness and run count stay empty (`load_test_history`; test `test_load_test_history_family_fallback`). A test without statistics that ran in the previous run (e.g. an untrusted run, which updates no `test_stats`) keeps that outcome and is not scored as a new test.

`run_events` and `test_results` are written in batches by `EventSink` (`orchestrator/runner.py`): every 100 ms or 200 pending rows, under the run's lock, with every payload string passed through `coverset.logging.redact`.

## The facts store

### Layout

```
<data_dir>/facts/
  repo=<repo_id>/
    dictionary.json              ids for paths, node ids and shape keys (append-only)
    dictionary.lock
    run=<run_id>/
      .lock                      writers of this run serialise on it
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

Paths, node ids and shape keys are stored as dense integer ids. The ids are list positions in `repo=<id>/dictionary.json`, so an id never changes once assigned and new values are only appended (`facts/dictionary.py`). Test id 0 is reserved for `<import>`, the pseudo test that holds lines executed outside any test (coverage.py's `''` context). Extensions happen under an exclusive `flock` and an atomic write, so concurrent ingests never hand out the same id twice. Garbage collection never touches the dictionary. Ids are therefore stable across runs of a repo (`test_ids_are_stable_across_runs`).

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
| `functions` | `commit_sha`, `func_key`, `file_id`, `path`, `qualname`, `ordinal`, `def_line`, `body_start`, `end_line`, `body_hash`, `sig_hash`, `params_json`, `kind` (`function`, `method`, `class`, `module`), `language` | the head commit's regions, indexed once per run |
| `runs_dim` | `head_sha`, `base_sha`, `branch`, `pr_number`, `created_at`, `scoring_basis_run_id`, `expected_shards`, `ingested_shards`, `coverage_complete`, `finalized_at` | mirrored from the control database so facts SQL never attaches SQLite |
| `file_cov` | `file_id`, `path`, `statements`, `covered`, `branches`, `branches_covered`, `partial_branches`, `percent` | `percent = 100 × (covered + branches_covered) / (statements + branches)` |
| `func_input_cov` | `func_key`, `tests_called`, `tests_sampled`, `tests_truncated`, `blocks_expected`, `blocks_observed`, `missing_json` | input-class coverage per function; the class columns stay null without a catalogue. `tests_truncated` is counted over the whole run with `analysis/shapes.cap_hits`, the definition the verdict and the UI use: 0 when no shard capped the function, else the tests that called it (lines in its body) but were never sampled, at least 1 |

Line numbers are `int32`. coverage.py arcs use negative numbers for code-object entry and exit, so arc columns allow them.

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

There is no DuckDB database file. Every query opens a fresh in-memory connection (`FactsStore.connect`):

1. `duckdb.connect` with `autoinstall_known_extensions` and `autoload_known_extensions` off.
2. `SET allowed_directories` to the facts root and `SET enable_external_access = false`.
3. One view per requested table: `read_parquet([<explicit list of committed files>], hive_partitioning=true, union_by_name=true, hive_types={'repo': BIGINT, 'run': VARCHAR})`, with `repo_id` and `run_id` taken from the partition directories. A table with no files gets an empty view of the right schema.
4. `SET lock_configuration = true`.

Views can be scoped to one repo, one run, or a list of runs, and to the tables a query needs, which keeps a per-request connection cheap. Because nothing holds a lock on a database file, the CLI, scripts, tests and screenshots can query while `coverset serve` ingests. A query that hits a file replaced mid-read is retried once.

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

As an example, on the PR #2 run of a local demo, `SELECT count(DISTINCT tl.test_id) FROM functions f JOIN test_lines tl ON tl.file_id = f.file_id AND tl.line BETWEEN f.body_start AND f.end_line WHERE f.qualname = 'validate_email' AND tl.test_id <> 0` returned 24, and `file_cov` showed `src/ledger/accounts/emailcheck.py` and `src/ledger/billing/allocate.py` at 100.0% line and branch coverage.

## Retention and gc

| Setting | Default | What it governs |
|---|---|---|
| `COVERSET_RETENTION_RUNS_DAYS` | 30 | runs, their rows and their facts directories |
| `COVERSET_RETENTION_EVENTS_DAYS` | 7 | `run_events` rows |
| `COVERSET_RETENTION_DELIVERIES_DAYS` | 14 | webhook payloads are nulled after this; the rows stay at least 30 days so redeliveries are still deduplicated |

`coverset gc [--dry-run]` (`commands/maintenance.py`) deletes runs older than the cutoff that are not queued or running, deletes old `run_events`, nulls old delivery payloads, deletes delivery rows past `max(30, COVERSET_RETENTION_DELIVERIES_DAYS)` days, and removes expired facts directories with `facts/gc.py` (`expire_runs`). It always keeps the latest completed baseline per repo and the latest run per open PR. A facts run's age is its `runs_dim.created_at`, else the directory's mtime. A run directory is renamed to a hidden name before deletion, so readers never see half a run.

`coverset repo purge NAME` deletes a repository's rows, its facts directory and its mirror.

The same pass (`orchestrator/history.py`, `collect_garbage`) runs in-process: `serve` starts it as a Quart background task when it starts serving and then every 24 hours (`app.py`, `schedule_gc`; stopped by the app's shutdown event). After each PR run the runner applies the per-PR rule (`history.expire_pr_runs`): the PR's latest run stays, its other runs older than `COVERSET_RETENTION_RUNS_DAYS` are deleted with their facts. Both take a module-level lock without waiting, so a second gc in the same process skips instead of racing, and neither runs under `COVERSET_ENV=test` unless a test invokes it.

## The NeutralBundle contract

`facts/ingest.py` consumes only `coverset.lang.base.NeutralBundle`, never a pytest-specific file. A language adapter's `read_bundle(bundle_dir)` returns one per shard:

| Field | Type | Meaning |
|---|---|---|
| `meta` | `dict` | free-form; `meta["convert"]["bundle_sha256"]` is the bundle's identity when present |
| `collected` | `tuple[CollectedItem, ...]` | `nodeid`, `file`, `line`, `markers`, `invariant_ids`, `hypothesis` |
| `tests` | `tuple[TestRow, ...]` | `nodeid`, `file`, `outcome`, `duration_s`, `failed_phase`, `fail_sig`, `longrepr`, `start`, `stop` |
| `coverage` | `dict[str, dict[str, tuple[int, ...]]]` | test id → path → executed statement first lines; the key `<import>` holds lines run outside tests |
| `static` | `dict[str, StaticFile]` | `path`, `n_lines`, `statements`, `excluded`, `arcs_possible` |
| `run_arcs` | `dict[str, tuple[tuple[int, int], ...]]` | path → executed branch arcs, union over tests |
| `shapes` | `tuple[ShapeObs, ...]` | `func` triple, `param`, `shape_key`, `caller` (a triple or a test id), `samples`, `tests`, `truncated`, `example`, `example_test` |
| `sampling` | `tuple[FuncSampling, ...]` | `func` triple, `calls`, `tests`, `truncated` |
| `durations` | `dict[str, float]` | seconds by test id |

Paths are git-root-relative POSIX. Test ids may be any string up to 512 characters. Shape keys must follow the grammar in [docs/instrumentation.md](instrumentation.md#shape-keys); a new language either maps its values onto those type tokens or extends the catalogue ([docs/extending.md](extending.md#adding-a-language)).

The plan also describes an on-disk neutral format (`tests.jsonl`, `coverage.jsonl`, `shapes.jsonl`, `invariant_links.jsonl`) that a Jest or Vitest reporter could write. That file format is **designed, not built**: today the contract is the in-memory dataclass, and a new adapter converts its own files into it.

## Measured ceiling

`scripts/bench_facts.py` generates synthetic shard bundles at the plan's scale (2,000 files, 200,000 statement lines, 10,000 tests × 2,000 lines per test in 8 shards, 30 runs, 1,000,000 `call_shapes` rows) and times conversion, ingest and finalisation, plus the p50 and p95 latency of `line_tests`, a 500-line `patch_coverage`, `treemap` and `shapes_for_function`. `--out docs/numbers.json` merges its `facts_*` keys into that file. The measured figures are in [docs/benchmarks.md](benchmarks.md); this page quotes none.

## Scale-out path

**Designed, not built.** When one machine's Parquet directory stops being enough, three steps apply in order, each behind the same `FactsStore` and `facts/queries.py` functions:

1. **Content-addressed per-(test, blob) bitsets.** Most tests execute the same lines of an unchanged file run after run. Store one bitset of executed lines per `(test, file blob sha)` and reference it from each run, so an unchanged file costs one row per run instead of one row per line.
2. **DuckLake.** Keep the Parquet files, move the catalogue (which files belong to which run and attempt, today `ingest_log`) into a DuckLake catalogue, so several control planes can read and write the same facts.
3. **Postgres with `pg_roaringbitmap`.** Store the per-(test, blob) bitsets as roaring bitmaps in Postgres, which makes "tests covering line L" and patch coverage set operations inside the database.

The control database moves to Postgres through `COVERSET_DATABASE_URL`; that path is untested.
