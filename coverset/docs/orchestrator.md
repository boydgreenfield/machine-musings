# The orchestrator

How a coverset run goes from a queued row to a verdict on the pull request, what each step records,
and what a new executor backend has to implement. Code: `src/coverset/orchestrator/` (queue, planner,
runner, verdict, bundles, history, repos, budget, pool, reaper) and `src/coverset/executors/`.

## How a run flows

```
queued ──▶ claim + policy ──▶ plan ──────────────────────────────▶ shards ─────────────▶ ingest ──▶ report ──▶ verdict
           (queue.py)          mirror → archive → env → workers →    events, retries,      bundles     patch,       stored,
                               put_tree → collect → diff → prior →   fail-fast, STOP       → facts     input        posted,
                               scores → wave 1 → shards → files      (runner.py)           (bundles)   classes,     done event
                               (planner.py)                                                            invariants
```

1. **Queue.** A run row is created `queued` by the forge intake (webhooks) or `coverset run --no-wait`.
   The dispatcher (`RunDispatcher`, installed on the web app as `app.extensions["run_queue"]`) claims
   queued runs oldest first while fewer than `COVERSET_MAX_CONCURRENT_RUNS` execute; a queued run of a
   PR that already has a newer queued or running run is cancelled instead (`superseded by run <id>`,
   with `COVERSET_CANCEL_SUPERSEDED=true`). Claiming is an atomic
   `UPDATE ... WHERE status = 'queued'` that also sets `runs.claimed_by` (`<role>@<host>:<pid>:<nonce>`,
   role `serve` or `cli`) and `runs.heartbeat_at` (refreshed every 10 s while the run executes), so a
   run never executes twice. Inline runs (`coverset run`, `coverset demo`) are inserted already
   `running` and claimed by their CLI process, so a `serve` sharing the database never picks them up.
   `COVERSET_PAUSED=true` starts nothing.
   At boot, `serve` handles what earlier processes left: `queued` runs become `abandoned` (or stay
   queued with `COVERSET_REQUEUE_ON_BOOT=true`); a `running` run is taken over only when its executor
   is gone (claimed by an earlier `serve` on this host, by a process on this host that no longer
   exists, by nobody, or with a heartbeat older than 5 minutes), never while a live CLI runs it. A
   taken-over run becomes `abandoned` with `verdict` and `done` events; with
   `COVERSET_REQUEUE_ON_BOOT=true` a fresh queued copy (same trigger, PR, `extra_env`, webhook arrival
   time; `timeline.requeued_from` names the old run) is created first, since the old run's shard and
   result rows already exist.
   Upload runs (`trigger = "upload"`, [upload.md](upload.md)) never pass through the dispatcher:
   they are created `running` by `POST /v1/bundles` and processed by the serving process's
   `UploadWorker` (`orchestrator/upload.py`), never executed, so there is nothing for a worker to
   run and no executor to lose. The boot takeover therefore skips them entirely: an upload run
   waiting for the rest of its matrix legs (`--shards N`) is `running` by design and stays so across
   a restart, neither abandoned nor re-queued, and the next leg's upload joins it. After the
   takeover, `install_run_queue`'s start-up hook calls `resume_upload_runs`, which schedules one
   pass on the app's `UploadWorker` for every upload run still `running`: a pass that a restart
   interrupted concludes its run, and a run still waiting for legs keeps waiting. The check run of an upload run carries no re-run buttons, since a managed
   re-run would need an executor the repository may not have (`forge/verdict.py`, `verdict_actions`).
2. **Policy.** Untrusted runs never use the local executor; webhook-triggered runs use it only with
   `COVERSET_LOCAL_ALLOW_WEBHOOK_RUNS=true`. A Modal run is admitted only if today's spend plus what
   it reserves fits `COVERSET_MODAL_DAILY_BUDGET_USD`; otherwise it concludes `neutral` with reason
   "budget exhausted". Today's spend (UTC) is today's `runs.cost_estimate`, which includes the
   reservations of runs still in flight, plus today's `executor_spend` rows (pool idle time, snapshot
   builds, smoke tests, image builds: spend that belongs to no run). Before planning a run reserves one
   worker for 10 minutes; after planning it is re-admitted at its worst case, every worker for the
   full `COVERSET_RUN_TIMEOUT_S` at the limit rate, capped by `COVERSET_MAX_RUN_COST_USD` (1.00), and
   refused ("budget exhausted") if that no longer fits. The reservation stays until the real cost
   replaces it, so concurrent runs see each other's spend. The cap is also enforced live: on every
   progress tick and heartbeat the runner prices the workers held so far, and once that passes the
   cap the shards get STOP files and are cancelled, a `log` event (`warning: cost_cap`) is written and
   the run ends `error`, `neutral`, "run cost cap $X reached", with its real cost recorded. The cancel
   watcher starts right after the claim, so cancelling or superseding a run also interrupts its
   planning.
3. **Plan.** The repo's bare mirror (`<cache_dir>/mirrors/<repo_id>.git`) is refreshed; the head commit is
   exported with `git archive` into a temporary control-plane checkout. The environment spec is the
   repo's pinned requirements plus the pytest-coverset wheel (`executors/env_export.py`), and the
   executor builds or finds it (`ensure_env`); see [The environment](#the-environment) for where the
   requirements come from and how the project itself gets installed. An untrusted run never builds an environment the executor does
   not have (Modal's image builder has network access): it is `skipped`, `neutral`, "untrusted PR
   changes the environment: approve to build" until a maintainer approves it, and refused even then
   when the requirements carry direct URL, VCS or index references (`planner._untrusted_env`). A
   forced shard count (`coverset run --shards N`) is capped by `COVERSET_MAX_SHARDS` before any worker
   is acquired. Executors with snapshots use the newest ready trusted snapshot;
   a PR never waits for one. Workers are acquired (as many as the repo's timed tests need), each gets
   the archive (`put_tree`), and `sandbox_ready` is recorded. Worker 0 collects
   (`pytest --collect-only --continue-on-collection-errors` with the plugin, every plugin option one
   word such as `--coverset-out=<dir>`, so pytest's early ini lookup keeps the repo's own config;
   `collect.json` is read back, the `-q` listing is the fallback). Exit codes 0, 1, 2 and 5 are
   accepted whenever `collect.json` parses: a test module that fails to import is listed in its
   `errors`, kept in `plan.collect_errors`, and becomes a confirmed failure of the run (a failure
   row named by the module path with the import error, reason "N collection error(s)", verdict
   `failure`, exit code 1) while every other test still runs; with nothing collected but errors the
   run fails the same way instead of "no tests collected". Collected node ids that `@shard.args`
   could not hold on one line (any line break `str.splitlines()` knows, or NUL; possible only with
   pytest's id escaping disabled) are dropped with a warning. Sources are decoded as CPython does
   (PEP 263 coding cookie or BOM; undecodable bytes become U+FFFD). Then the diff `base...head` is
   classified into changed functions, the prior run is chosen (the PR's previous head, else the
   merge-base, else the latest default-branch baseline; a candidate needs usable facts:
   `coverage_complete`, or at least one committed shard ingest), every test is scored (a changed
   path matching `[tool.coverset] tracked_globs`, default `uv.lock` and `conftest.py`, matched
   from the right like `PurePath.match`, gives every test the `impacted_by_diff` signal with the
   explanation "tracked file changed: <path>"; wave 1 and sharding do not change), wave 1 (the change-covering set) is computed and the tests are dealt into
   shards (`analysis/sharding.py`, `plan_shards`): `K = ceil(Σ p50 / target_shard_seconds)`, capped by
   `max_shards`, by the number of assignment units (a fixture group in one priority decile is one
   unit) and by `max(1, n_tests // 3)` unless the *measured* p50s alone exceed `target_shard_seconds`
   times that many shards. A test nobody has timed counts as the repo's median p50, or 0.05 s when no
   test has been timed yet, so the first baseline of a small suite runs in one shard instead of
   fanning out (14 untimed tests used to become 7 shards). `--shards N` asks for `N` shards (still at
   most one per assignment unit) and lifts the three-tests-per-shard cap. Wave-1 tests are dealt first, one to each least-loaded shard, then the
   rest in priority order (longest-processing-time balancing); an empty shard is dropped, because
   pytest given an empty args file runs the whole suite. If fewer workers are available than shards, the plan is
   re-dealt over the workers obtained; none at all fails the run with reason `capacity` (the local
   executor first waits up to 10 minutes for another run to release one).
   Planning holds no write transaction while it works (the environment build and collection can
   take minutes): it only reads, the `envs` and `tests` rows are written in their own short
   transactions, and the plan's run fields, `shards` and `test_results` land in one short final
   transaction together with whatever events the run queued meanwhile. Scoring, wave 1, sharding,
   function indexing, YAML and shape catalogues run in threads, off the event loop.
4. **Shards.** Each worker gets `run/shard.args` (its nodeids, in priority order),
   `run/shard.scores.json` (rank-derived scores that make the plugin reproduce that order) and
   `run/full-trace.txt` (the PR's changed source files plus every invariant target's file, traced
   without a sample cap), and runs the adapter's test command, with `[tool.coverset] pytest_args`
   appended last (collection appends them after the test paths), as `coverset doctor` shows.
5. **Events.** Stdout lines that start with `\x1ecoverset <nonce> ` (a fresh 128-bit nonce per exec in
   `COVERSET_EVENT_NONCE`) are events; anything else is log text. Lines are capped at 1 MiB, an
   exec's output at `COVERSET_MAX_STDOUT_BYTES` (then it is killed and the shard errors).
6. **Retry and fail-fast** (below), then **ingest**: every shard's `bundle.tar.gz` is pulled before
   its worker is released, then extracted, converted and ingested concurrently (a process pool;
   ingest serialises on the facts run lock). `results.jsonl` in the bundle is authoritative and
   overrides streamed outcomes; retried tests keep their retry verdict. The run is finalized
   (`file_cov`, `func_input_cov`, `coverage_complete`, and the test-economy tables with the
   repository's scoped `policy.test_tax_threshold` and the tests' `test_stats` p50s as durations:
   `runner.economy_inputs`, which upload runs use too). In `serve` the web's run-facts and
   source-text caches are then warmed for the run in a background thread
   (`runner.schedule_warm_up`, `web/loaders.py` `warm_run`), so the first page after an ingest is
   not a cold one; a warm-up failure is logged and never touches the run.
7. **Report and verdict.** Patch coverage and the changed-function table, input-class coverage of
   the changed functions and of every invariant target (classes no test ever passed in, each with a
   Hypothesis snippet), invariant statuses (head definitions plus base definitions the PR removed,
   with their coverage lens) and the PR's changes to `invariants.yaml`, the optional AI review
   (advisory; its absence or failure is recorded as `ai_source = unavailable`), `test_stats`
   (recomputed over each test's last 50 executed results), and the cost estimate. The terminal status,
   the verdict (`runs.verdict/reason/summary_json`) and the `verdict` and `done` events are written in
   one transaction, retried with backoff after a deadlock or a serialization failure; then the verdict is posted to the forge
   for PR runs (check run + sticky comment). A report that cannot be built degrades to a verdict
   without its extras rather than failing the run.
8. **Afterwards.** A PR run applies the per-PR retention rule (outside `COVERSET_ENV=test`). A trusted
   baseline (`baseline` or `push` trigger, no PR) of the repo's default branch that completed with
   verdict `success` or `failure`, on an executor whose `capabilities().fs_snapshot` is true (Modal),
   schedules a background re-snapshot: if its head is still the branch tip in the mirror,
   `executor.ensure_snapshot(env, repo, head_sha)` builds (or finds) the trusted snapshot and records
   the `snapshots` row, then the warm pool of that repo is refilled from it when `serve` maintains one
   (see below). PR runs, untrusted runs, errored or cancelled runs, other branches and superseded heads
   (the demo replaying older `main` commits) never snapshot, and a failed build (budget, capacity) is
   logged and leaves the previous snapshot in use. The task lives in
   `OrchestratorContext.background`; a CLI context waits for it before closing its executors, so
   `coverset run --executor modal` of the default branch's tip builds the snapshot before it exits.
   The same baselines, on every executor, also get **`coverset invariants scan`** in the background
   (`runner.scan_after_baseline`, `COVERSET_SCAN_AFTER_BASELINE`, default true): if the head is still
   the branch tip, the scan (see Commands) runs pinned to that run, review-kind invariants get a
   fresh advisory review through the configured client (with `COVERSET_AI=off` there is none and the
   statuses are still recorded: `needs_review` for one verified only by review), and their rows are
   rewritten with `trigger = 'scan'`. Errors are logged, never raised into the run.

Every failure path (planning, environment, capacity, an internal error, cancellation) ends the run
`error` or `cancelled` with a reason, a `verdict` event and a `done` event, so SSE clients and
`coverset-ci wait` always finish. If even that write fails, or the run's task dies, a last-resort
finalizer (in the runner, the dispatcher and the CLI) marks the run `error` with both events and
replaces the budget reservation in `cost_estimate` with the real worker cost (what the dead
execution in that process had held, else 0: nothing was acquired), so a crashed Modal run does not
keep its reservation against the day's budget. Worker-produced text (`longrepr`, collection errors,
shard errors quoted in a reason) is redacted with `coverset.logging.redact` as it enters the run,
before it reaches `runs.reason`, `runs.summary_json`, events or the forge.

`done.exit_code` (what `coverset-ci wait` and `coverset run` exit with): `0` success, `1` a confirmed
failure or a collection error (also when shards errored too), `5` no tests collected, `2` for
everything else (error, cancelled, abandoned, neutral such as "budget exhausted" or a skipped run).
`coverset-ci wait` stops at the first `failure` verdict (exit 1); after any other verdict it reads
on to `done` and exits with its code.

**Cost.** `runs.cost_estimate = Σ executor.cost_usd(resources, seconds the worker was held)` over
every worker the run acquired, from `acquire` to `release` (extras released right after sharding
count for the time they were held), recorded on every terminal path. Local runs cost 0. Spend that
belongs to no run is in `executor_spend` (see Policy above).

## Events

`run_events` rows are written in batches (every 100 ms or 200 rows) with consecutive `seq` numbers;
payload strings are redacted. A transaction that adds its own event (the `verdict` and `done` rows)
also writes every event queued while it ran, in the same commit: those were numbered below its own
row, so a client reading `seq > since` after the commit can never skip one that commits later. Kinds from the HTTP contract (Addendum A): `progress`, `test`, `log`,
`verdict`, `done`. Internal kinds: `queued` (intake), `status` (`running`, `planned` with test,
shard and wave-1 counts and the prior run), `shard` (per-shard status and exit code) and `failure`
(a confirmed failure: retried or not, pre-existing or not, its rank). The first-failure verdict is a
`verdict` event with `"early": true`; the final one follows after ingest.

## Timeline

`runs.timeline` holds ISO 8601 UTC times:

| key | when |
|---|---|
| `webhook_received` | the forge delivery arrived (webhook runs only) |
| `queued` | the run row was created |
| `sandbox_ready` | every worker holds the source (before collection) |
| `first_test` | the first test result streamed in |
| `wave1_finished` | the last wave-1 test reported (at `first_test` when wave 1 is empty) |
| `first_failure` | the first confirmed, non-pre-existing failure |
| `verdict` | the first verdict was stored (the early failure verdict, else the final one) |
| `finished` | the run reached its terminal status |

The verdict's clock starts at `webhook_received`, else `queued` (`t0`). `first_failure_s =
first_failure - t0`. `rank_coverset` is the failing test's **planned** position in the global
priority order (wave 1 first, then by score); `rank_executed` is the position at which it actually
reported: how many tests (across all shards, first attempts) had reported a result when it did. The
two differ when shards run in parallel or when the plugin keeps a module's or class's tests together
behind their best-ranked member. `rank_file_order` is its position in plain collection order, and
`t_first_failure_file_order_est = (first_test - t0) + Σ p50` of the tests before it in file order:
an **estimate** of when plain file-order CI would have reached it. It starts at the first test
because the environment, collection and planning happen in either order (`sandbox_ready` when no
result streamed in); unknown p50s count as the median of the known ones, or as 0.05 s (the
planner's default) when no test has been timed. It is labelled "estimated from the first test" on
the run page, the PR page, the sticky comment and in `runs.summary_json`
(`t_first_failure_file_order_est_label`).

## Retries, flaky tests and fail-fast

Fail-fast lives in the orchestrator, never in the plugin. When a non-quarantined test reports
`failed` or `error` for the first time, the runner starts one retry exec on the same worker, alongside
the shard: `--coverset-out=run/retry-<n> --coverset-shapes=off`, the nodeid alone in
`run/retry-<n>.args`. Retries are never ingested and are capped per run by
`COVERSET_RETRY_BUDGET_PER_RUN` (a failure past the budget is confirmed without a retry).

* The retry passes: the test is **flaky** (`test_results.flaky = true`, outcome `passed`, two
  `test_attempts` rows); it does not fail the run.
* It fails again: the failure is **confirmed**. If the same `fail_sig` (exception type + crash
  location) failed in the base-branch run, it is **pre-existing** and does not count (a run whose
  only failures are pre-existing succeeds with a reason such as "12 passed, 1 pre-existing failure,
  2 skipped": only passed tests are counted as passed). Otherwise the
  first confirmed failure records `rank_coverset` and `rank_executed`, sets `timeline.first_failure` and posts a
  `failure` verdict immediately; later ones are added to the final verdict.

`[tool.coverset] fail_fast` picks what happens next:

* `finish_in_background` (default): the shards run to completion, so coverage and shapes land.
* `cancel_remaining`: the runner writes `run/STOP` into every running worker (the plugin stops
  before its next test, writes its bundle and exits 2), waits up to 20 s for the bundles, then
  cancels what is left. Exit code 2 with STOP is a normal shard end; the tests that never ran are
  counted in `runs.cancelled_tests` and the partial facts are ingested.

A newer queued or running run for the same PR cancels the current one when
`COVERSET_CANCEL_SUPERSEDED=true` (reason `superseded by run <id>`); so does setting a run's status to
`cancelled`. Cancelled runs are not ingested.

Shard exit codes: 0/1 are normal shard ends; 5 (no tests) concludes the run `neutral` ("no tests
collected", `done.exit_code = 5`); 2 without a STOP request, any other code, a timeout or the output
cap are shard errors (`status = error`, conclusion `neutral` unless a confirmed failure exists). The
verdict notes shards whose pytest exit code disagrees with the results they reported.

## The `extra_env` / retry contract

`runs.extra_env` (a JSON object) is applied to every exec of the run: collection, every shard and
every retry. `runs.retry_increment_keys` lists keys whose integer value a retry exec increments by
one. Nothing else from the control plane's environment reaches a test, and a run id is never passed.

The demo uses this to own its deliberately flaky test: history run *n* of `main` sets
`COVERSET_DEMO_RUN=n` with `retry_increment_keys = ["COVERSET_DEMO_RUN"]`, so on main-4 the test fails
(`4 % 3 == 1`), its retry sees `5` and passes, and the failure is recorded as flaky. PR runs leave
`extra_env` empty.

## The environment

Where the requirements come from (`lang/python_env.py`, called by `PythonAdapter.env_spec`; the first
that applies wins): `[tool.coverset] requirements`; `uv.lock` (`uv export --frozen --offline
--no-hashes --no-emit-project --no-emit-workspace --no-emit-local --all-groups`, plus `--all-extras` when
the project declares extras); `poetry.lock` and `Pipfile.lock`, converted by coverset with their
markers; requirement files found by name (`requirements*.txt`, `requirements/*.txt`; minimum-version and
docs files skipped); `[project]` metadata with every extra and the test/dev dependency groups;
`setup.cfg`/`setup.py` literals read with `ast`. The last four are pinned with `uv pip compile --universal
--no-build`, offline first against coverset's own uv cache, and cached by the sha256 of the resolved
inputs under `~/.cache/coverset/requirements/`, so the same inputs always pin the same versions and only
the first resolution of new inputs needs the network. Nothing from the repository runs on the control
plane: no `setup.py` is executed, the project is never resolved, `--no-build` refuses sdist-only
dependencies, index options in requirement files are dropped, and direct URL/VCS references are never
handed to the resolver (only requirements by name are compiled; the references are appended verbatim,
and `file:` URLs and filesystem paths are refused outright), so an untrusted PR cannot make the control
plane fetch anything before `_untrusted_env` applies its approval and direct-reference gate. `EnvSpec.env_source` records the
source (`uv.lock`, `requirements`, `poetry`, `pipenv`, `pyproject`, `setup`); the requirements text of
every source but `uv.lock` starts with a header naming it and the digest of the resolved inputs, so
`env_hash` changes exactly when they do (the hashes of uv-locked repositories are unchanged).

The project is imported from `PYTHONPATH=<source_root>:<repo>` and normally never installed.
`EnvSpec.install` (`[tool.coverset] install`, default `auto`) is `wheel` or `editable` for projects that
only work installed (entry points they read themselves, metadata read at import, generated version
files, compiled extensions); then the backend's `[build-system] requires` join the requirements, and
the project is installed with `--no-deps --no-build-isolation`: by Modal when it builds a trusted
snapshot, before collecting, and by the LocalExecutor per (environment, commit) into
`~/.cache/coverset/installs/<key>/site`, which the run's workers get as a site directory. Packages that
live in the repository (committed wheels, path dependencies, workspace members) are recorded as
`# coverset-local:` lines and installed in the same step. `PYTHONPATH` still comes first; an import hook
written next to the install serves only compiled extension modules and build-generated modules from the
installed copy. `coverset doctor --repo PATH` shows every one of these decisions for a repository;
[docs/getting-started.md](getting-started.md) has the matrix, the keys and the caveats.

## The LocalExecutor is a convenience, not a sandbox

The LocalExecutor runs a PR's tests as your user, on your machine, with your network. It scrubs the
child environment (built from scratch: `PATH=<venv>/bin:/usr/bin:/bin`, `HOME` and `TMPDIR` inside the
worker's private directory, `PYTHONPATH`, `PYTHONUNBUFFERED`, `LANG`, `TZ`, the plugin's `COVERSET_*`
options and the run's `extra_env`; never `COVERSET_GITHUB_*`, `ANTHROPIC_API_KEY`, `MODAL_TOKEN_*`,
`VIRTUAL_ENV`, `PYTEST_*` or `COVERAGE_*`), starts every exec in its own session (cancel =
`SIGTERM` to the group, `SIGKILL` 5 s later; when an exec ends, and when its worker is released,
whatever is left in its process group is killed, so a test suite's background processes do not
outlive it), extracts source with the tarfile `data` filter, keeps each worker in its own
`coverset-run-*` temp directory (removed on release, after which every worker call raises; leftovers
older than an hour are swept at start-up, except a directory whose `.worker.lock` a live worker
holds with `flock`, or whose `run/` or `repo/` changed within the hour) and caches one virtualenv per environment hash under
`~/.cache/coverset/venvs/` (built under a file lock and renamed into place atomically). `uv` builds
the venv with `--no-config` and an allow-listed environment (`PATH`, `HOME`, cache dirs, `UV_*`,
proxy and TLS variables: no tokens reach an sdist's build hooks), installs the requirements, then
the plugin wheel with `--no-deps`. At most `COVERSET_LOCAL_MAX_WORKERS` workers exist at once; a run
that holds none waits for one (up to 10 minutes) rather than failing with `capacity`. None of that
stops hostile test code from reading your files or calling out. Untrusted PRs never run on it,
and webhook-triggered runs need `COVERSET_LOCAL_ALLOW_WEBHOOK_RUNS=true`.

## Writing a new executor backend

Implement `coverset.executors.base.Executor` and `Worker` (and register the kind in
`coverset.executors.get_executor`):

| method | contract |
|---|---|
| `ensure_env(EnvSpec) -> EnvRef` | build or find the environment for `spec.env_hash`: Python `spec.python`, `spec.requirements_txt`, then the plugin wheel `spec.plugin_wheel` **without its dependencies** (`build_env_spec` already appended `coverage==<coverset's locked version>`, and `pytest==...` when the repo has no pytest, unless the repo's lock pins them; those pins are part of `env_hash`; the project itself is never installed into the environment) |
| `ensure_snapshot(env, repo, sha) -> SnapshotRef \| None` | a trusted filesystem snapshot of a default-branch commit, or None when unsupported; when `install_needed(env.spec)` (`lang/python_env.py`), install the project and the repository's local packages into it first (`snapshot_install_commands`) |
| `install_project(env, checkout, *, sha, run_id)` (optional) | for executors without snapshots: install the head's project and local packages for that run's workers; the planner calls it for trusted runs when `install_needed(spec)` |
| `acquire(base, n, resources, ttl_s, labels) -> list[Worker]` | up to `n` workers (fewer under capacity pressure; raise `CapacityError` only when none at all can be had). `resources.network` is `blocked` for untrusted runs. `labels` carry `coverset_run`, `repo`, `role` for reaping |
| `capabilities()` | `egress`, `warm_pool`, `fs_snapshot`, `max_workers`, `name` |
| `cost_usd(resources, seconds)` | the rate card; the runner calls it with each worker's lifetime (acquire to release) and sums it into `runs.cost_estimate` and the daily budget, and prices reservations with it |
| `close()` | release everything |
| `Worker.path(rel)` | the absolute in-worker path of a worker-relative path |
| `Worker.run(cmd, *, cwd, env, timeout_s, on_line, stdout_cap) -> RunResult` | run in `cwd` (worker-relative) with **only** the fixed base environment plus `sanitize_env(env)`; call `on_line(text, "stdout" \| "stderr")` per line as it arrives (cut at 1 MiB); kill on timeout, on `stdout_cap` (then `truncated=True`) and on `cancel()` (then `cancelled=True`); an exception raised by `on_line` must not stop reading; never raise for a non-zero exit |
| `put_tree(tar_gz, dest)` / `get_file(path)` / `write_file(path, data)` | file transfer relative to the worker root; refuse absolute paths, `..`, links out of the tree |
| `cancel()` / `release()` | stop running execs (graceful signal first: the plugin writes its bundle on SIGTERM); release frees the worker, idempotently; after release every other method raises `ExecutorError` |

Workers never run git against a remote and never receive forge credentials: source arrives only
through `put_tree`. Backends whose workers can outlive the control plane implement
`ReapingExecutor.reap(active_run_ids=..., all=...)` (used by `coverset modal reap` and the serve
loop); backends with warm pools implement `PoolingExecutor.fill_pool/drain_pool`
(`orchestrator/pool.py` calls them only when `capabilities().warm_pool` is true).

## Serving: the warm pool, the reaper and re-snapshots

`create_app` starts these only outside tests (`app.schedule_executor_upkeep`; tests call it
explicitly): at startup when `COVERSET_EXECUTOR=modal`, and otherwise as soon as the serving process
builds a Modal executor (`OrchestratorContext.on_executor_built`), e.g. for a run queued by
`coverset run --no-wait --executor modal`, so such a run never executes without the reaper and the
pool. Both loops use the executor instance the app's runs use
(`OrchestratorContext.from_app(app).executor(...)`), because the pool lives in that process.

| Loop | Every | What it does | Code |
|---|---|---|---|
| reaper | 60 s, starting with the Modal executor | terminates sandboxes tagged with this installation's `coverset_instance` whose `run` tag is not a queued or running run, that this process does not hold (pool, shards, snapshot builds), and that are not a pool, snapshot or smoke sandbox younger than its natural lifetime (another process's `warm` or `smoke`) | `orchestrator/reaper.py` (`reap_forever`) |
| pool maintainer | 30 s, starting when serving starts | for each repo with a run on this executor in the last `COVERSET_POOL_TTL_S` (600) seconds, tops the pool up to `COVERSET_POOL_SIZE` (4) sandboxes from the repo's newest ready, unexpired trusted snapshot (a repo without one gets no pool); drains the pools of repos that went quiet; while today's spend has reached `COVERSET_MODAL_DAILY_BUDGET_USD` it fills nothing and drains every pool | `orchestrator/pool.py` (`PoolMaintainer`) |

Fills go through `ModalExecutor.fill_pool`, so they count against the sandbox semaphore of the
process (`COVERSET_MAX_SANDBOXES`, shared with shards and snapshot builds; one semaphore per
process) and its own budget guard. A pooled sandbox's idle cost is written to `executor_spend` when
it is claimed or terminated, so every process and the next restart see it. The runner calls `PoolMaintainer.refill(repo_id)` after
a re-snapshot, so a new snapshot replaces the pool at once (pooled sandboxes of an older image are
terminated). `COVERSET_POOL_SIZE=0` disables the maintainer. When serving stops, re-snapshots still in
flight are cancelled, the pool is drained and the executors are closed, so no sandbox this process
created is left running; anything a crash leaves behind is ended by Modal's `idle_timeout`
(`COVERSET_POOL_TTL_S`) and `timeout`, or by the next reaper pass.

## Commands

* `coverset run --repo PATH|URL --head SHA|branch [--base SHA|branch] [--pr N] [--executor local|modal] [--shards N] [--trigger pr|baseline] [--wait/--no-wait]`:
  registers the repository (local paths and `file://`/`https://` URLs without credentials), creates
  the run and executes it inline, streaming pytest-style lines; exits with pytest's codes.
* `coverset demo [--executor local|modal] [--port 8000] [--no-serve]`: seeds `data/demo.git`,
  registers it under the webhook fixtures' identity (`coverset-demo/ledger`), runs the six `main`
  commits as baselines and both PRs, replays `fixtures/webhooks/pr_opened.json` in-process (a
  duplicate of PR #1's run, so nothing reruns) and serves. Re-running reuses every completed run.
* `coverset invariants scan [--repo PATH|NAME] [--executor local|modal]`: reviews every invariant with
  a `verify: {kind: review}` entry at the head of the latest completed, trusted baseline run of the
  default branch whose facts exist (`--executor` restricts the run to that executor). Each target
  function becomes a review target in a bundle built from that run's facts (covering tests with their
  bodies, input classes, shapes, linked tests) and the head sources, reviewed with
  `get_review_client(settings, repo_enabled=...)` (the offline fake unless `COVERSET_AI=anthropic` and
  the repo opted in); new findings go into `review_findings` under that run (a matching
  `(func_key, bundle_hash)` is reused), and `analysis/invariants.evaluate` turns the review evidence
  into statuses written to that run's `invariant_results` rows with `trigger = 'scan'`. An invariant
  verified only by review becomes `satisfied` or `needs_review`; one that also has test, property or
  runtime entries keeps the status its tests decided in the run (a review never overrides tests and
  never yields `violated`) and gains the review entry in its evidence. Prints a table of invariant,
  target, verify kinds, review finding, the run's status and the scan's status. The runner runs the
  same scan after every trusted, completed baseline of the default branch's tip (step 8 above).
  Code: `commands/invariants.py`.
* `coverset doctor --repo PATH [--json] [--no-collect] [--offline] [--write-config]`: what coverset
  would do with a repository, without running its tests ([docs/getting-started.md](getting-started.md)).
* `coverset warm`, `coverset facts query SQL`, `coverset shapes FUNC [--markdown] [--suggest]`,
  `coverset invariants check`, `coverset gc [--dry-run]`, `coverset repo purge NAME`, `coverset modal reap [--all]`.
