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
2. **Policy.** Untrusted runs never use the local executor; webhook-triggered runs use it only with
   `COVERSET_LOCAL_ALLOW_WEBHOOK_RUNS=true`; Modal runs past `COVERSET_MODAL_DAILY_BUDGET_USD` (the sum
   of today's `runs.cost_estimate`, which includes the reservations of runs still in flight) conclude
   `neutral` with reason "budget exhausted". An admitted Modal run reserves its predicted cost (one
   worker for 10 minutes before planning, then the plan's predicted shard durations plus 30 s each)
   until its real cost replaces it, so concurrent runs see each other's spend. The cancel watcher
   starts right after the claim, so cancelling or superseding a run also interrupts its planning.
3. **Plan.** The repo's bare mirror (`data/mirrors/<repo_id>.git`) is refreshed; the head commit is
   exported with `git archive` into a temporary control-plane checkout. The environment spec is the
   repo's `uv export` plus the pytest-coverset wheel (`executors/env_export.py`), and the executor
   builds or finds it (`ensure_env`). Executors with snapshots use the newest ready trusted snapshot;
   a PR never waits for one. Workers are acquired (as many as the repo's timed tests need), each gets
   the archive (`put_tree`), and `sandbox_ready` is recorded. Worker 0 collects
   (`pytest --collect-only` with the plugin; `collect.json` is read back, the `-q` listing is the
   fallback). Then the diff `base...head` is classified into changed functions, the prior run is
   chosen (the PR's previous head, else the merge-base, else the latest default-branch baseline),
   every test is scored, wave 1 (the change-covering set) is computed and the tests are dealt into
   shards (never more shards than assignment units; an empty shard is dropped, because pytest given an
   empty args file runs the whole suite). If fewer workers are available than shards, the plan is
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
   without a sample cap), and runs the adapter's test command.
5. **Events.** Stdout lines that start with `\x1ecoverset <nonce> ` (a fresh 128-bit nonce per exec in
   `COVERSET_EVENT_NONCE`) are events; anything else is log text. Lines are capped at 1 MiB, an
   exec's output at `COVERSET_MAX_STDOUT_BYTES` (then it is killed and the shard errors).
6. **Retry and fail-fast** (below), then **ingest**: every shard's `bundle.tar.gz` is pulled before
   its worker is released, then extracted, converted and ingested concurrently (a process pool;
   ingest serialises on the facts run lock). `results.jsonl` in the bundle is authoritative and
   overrides streamed outcomes; retried tests keep their retry verdict. The run is finalized
   (`file_cov`, `func_input_cov`, `coverage_complete`).
7. **Report and verdict.** Patch coverage and the changed-function table, input-class coverage of
   the changed functions and of every invariant target (classes no test ever passed in, each with a
   Hypothesis snippet), invariant statuses (head definitions plus base definitions the PR removed,
   with their coverage lens) and the PR's changes to `invariants.yaml`, the optional AI review
   (advisory; its absence or failure is recorded as `ai_source = unavailable`), `test_stats`
   (recomputed over each test's last 50 executed results), and the cost estimate. The terminal status,
   the verdict (`runs.verdict/reason/summary_json`) and the `verdict` and `done` events are written in
   one transaction, retried with backoff while SQLite is busy; then the verdict is posted to the forge
   for PR runs (check run + sticky comment). A report that cannot be built degrades to a verdict
   without its extras rather than failing the run.

Every failure path (planning, environment, capacity, an internal error, cancellation) ends the run
`error` or `cancelled` with a reason, a `verdict` event and a `done` event, so SSE clients and
`coverset-ci wait` always finish. If even that write fails, or the run's task dies, a last-resort
finalizer (in the runner, the dispatcher and the CLI) marks the run `error` with both events.

`done.exit_code` (what `coverset-ci wait` and `coverset run` exit with): `0` success, `1` a confirmed
failure (also when shards errored too), `5` no tests collected, `2` for everything else (error,
cancelled, abandoned, neutral such as "budget exhausted" or a skipped run).

**Cost.** `runs.cost_estimate = Σ executor.cost_usd(resources, seconds the worker was held)` over
every worker the run acquired, from `acquire` to `release` (extras released right after sharding
count for the time they were held), recorded on every terminal path. Local runs cost 0.

## Events

`run_events` rows are written in batches (every 100 ms or 200 rows) with consecutive `seq` numbers;
payload strings are redacted. Kinds from the HTTP contract (Addendum A): `progress`, `test`, `log`,
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
`t_first_failure_file_order_est = (sandbox_ready - t0) + Σ p50` of the tests before it in file
order: an **estimate** of when plain file-order CI would have reached it (unknown p50s count as the
median), always labelled as estimated.

## Retries, flaky tests and fail-fast

Fail-fast lives in the orchestrator, never in the plugin. When a non-quarantined test reports
`failed` or `error` for the first time, the runner starts one retry exec on the same worker, alongside
the shard: `--coverset-out run/retry-<n> --coverset-shapes off`, the nodeid alone in
`run/retry-<n>.args`. Retries are never ingested and are capped per run by
`COVERSET_RETRY_BUDGET_PER_RUN` (a failure past the budget is confirmed without a retry).

* The retry passes: the test is **flaky** (`test_results.flaky = true`, outcome `passed`, two
  `test_attempts` rows); it does not fail the run.
* It fails again: the failure is **confirmed**. If the same `fail_sig` (exception type + crash
  location) failed in the base-branch run, it is **pre-existing** and does not count. Otherwise the
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
older than an hour are swept at start-up) and caches one virtualenv per environment hash under
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
| `ensure_env(EnvSpec) -> EnvRef` | build or find the environment for `spec.env_hash`: Python `spec.python`, `spec.requirements_txt`, then the plugin wheel `spec.plugin_wheel` **without its dependencies** (`build_env_spec` already appended `coverage==<coverset's locked version>`, and `pytest==...` when the repo has no pytest, unless the repo's lock pins them; those pins are part of `env_hash`; the project itself is never installed) |
| `ensure_snapshot(env, repo, sha) -> SnapshotRef \| None` | a trusted filesystem snapshot of a default-branch commit, or None when unsupported |
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

## Commands

* `coverset run --repo PATH|URL --head SHA|branch [--base SHA|branch] [--pr N] [--executor local|modal] [--shards N] [--trigger pr|baseline] [--wait/--no-wait]`:
  registers the repository (local paths and `file://`/`https://` URLs without credentials), creates
  the run and executes it inline, streaming pytest-style lines; exits with pytest's codes.
* `coverset demo [--executor local|modal] [--port 8000] [--no-serve]`: seeds `data/demo.git`,
  registers it under the webhook fixtures' identity (`coverset-demo/ledger`), runs the six `main`
  commits as baselines and both PRs, replays `fixtures/webhooks/pr_opened.json` in-process (a
  duplicate of PR #1's run, so nothing reruns) and serves. Re-running reuses every completed run.
* `coverset seed-demo`, `coverset warm`, `coverset facts query SQL`, `coverset shapes FUNC [--markdown] [--suggest]`,
  `coverset invariants check`, `coverset gc [--dry-run]`, `coverset repo purge NAME`, `coverset modal reap [--all]`.
