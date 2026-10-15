# coverset on Modal

coverset runs shards in [Modal Sandboxes](https://modal.com/docs/guide/sandbox), driven entirely
from the control plane with the Modal Python client. No functions are deployed: the App
`coverset-runner` is an empty app record that exists only as the container sandboxes are created in
(`modal app list` shows it as "deployed" because `App.lookup(..., create_if_missing=True)` creates a
persistent app). Code: `src/coverset/executors/modal_.py` (executor, workers, pool, reaper, guards)
and `src/coverset/executors/modal_images.py` (images).

## Setup

```sh
uv run modal setup                     # browser login; writes a token to ~/.modal.toml
uv run modal token info                # which workspace and token are active
uv run coverset modal smoke            # one sandbox end to end (well under a cent)
```

* **Profile.** The client reads the active profile in `~/.modal.toml`. On the machine coverset was
  built on that is `[boydgreenfield]` with `active = true`, so no `MODAL_PROFILE` is needed. Pick
  another profile with `MODAL_PROFILE=<name>`.
* **Environment.** Everything lives in the Modal environment `COVERSET_MODAL_ENVIRONMENT` (default
  `coverset`), separate from anything else in the workspace. Create it once:
  `uv run modal environment create coverset`. The App `coverset-runner` is created in it on first use
  (`modal.App.lookup(..., create_if_missing=True)`): an empty, persistent app record with no
  functions, which `modal app list` lists as deployed.
* **Image builder version.** Modal workspaces default to Image Builder Version 2023.12, whose
  `debian_slim` images stop at Python 3.12; the demo and most targets want 3.13. The client accepts a
  per-process override, and coverset sets `MODAL_IMAGE_BUILDER_VERSION=2025.06` (Python 3.10 to 3.14)
  before any image is defined (`modal_images.configure_builder`). An explicit value in your
  environment wins. Without it the 3.13 build is refused. You can also raise the workspace default
  in the Modal dashboard, which affects every app in the workspace.
* **CI.** Use a token instead of a profile: `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`, stored as CI
  secrets, plus `COVERSET_MODAL_ENVIRONMENT` if the environment is not `coverset`. coverset passes
  that environment name explicitly to every lookup, so `MODAL_ENVIRONMENT` does not choose it. These
  variables are read by the control plane only: the worker environment is rebuilt from scratch, and
  `MODAL_*` variables never reach a test.
* **Run something.** `uv run coverset demo --executor modal`, `uv run coverset run --repo PATH
  --head main --executor modal`, `uv run coverset warm --repo PATH --executor modal` (env image plus
  trusted snapshot of the default branch).

## How it works

### Images: one per environment

An environment is the target repo's pinned third-party requirements, the Python version from
`[tool.coverset] python`, and the pytest-coverset wheel; `env_hash` hashes all three. The
requirements come from the first source that applies (`lang/python_env.py`, the same for every
executor; [getting-started.md](getting-started.md#where-the-environment-comes-from)): `uv.lock`
(`uv export --frozen --offline`), `[tool.coverset] requirements` or the repository's
`requirements*.txt` files, `poetry.lock` or `Pipfile.lock` (converted without Poetry or Pipenv),
the `pyproject.toml` `[project]` dependencies with every extra and the test/dev groups, or
`setup.cfg`/`setup.py` read statically; all but the locks are pinned by `uv pip compile
--no-build`. Packages that live in the repository (committed wheels, path dependencies, workspace
members) are never requirements: each is recorded as a `# coverset-local: <file|dir|editable>
<path> <name>` comment line, so it is part of `env_hash` and the image install skips it. With
`[tool.coverset] install = "wheel"` or `"editable"` the build backend's `[build-system] requires`
join the requirements, so the project can be built later without network. Its image:

```python
modal.Image.debian_slim(python_version=spec.python)
  .apt_install("git")
  .env({"UV_COMPILE_BYTECODE": "1", "PYTHONUNBUFFERED": "1"})
  .uv_pip_install(requirements=[<requirements.txt>])
  .add_local_file(<wheel>, "/opt/wheels/<name>", copy=True)
  .run_commands("pip install --no-deps /opt/wheels/<name>")
```

Everything goes into the system interpreter; tests import the project through `PYTHONPATH`, and the
image never installs the project or the `# coverset-local:` packages (they come from a commit's
checkout: see Trusted snapshots). The plugin wheel is installed with `--no-deps`, exactly as the
LocalExecutor installs it: a target's lockfile need not pin `coverage`, so the environment export
appends an exact `coverage` pin (and `pytest` when the lock has none) to the requirements
(`env_export.with_plugin_pins`, part of `env_hash`). The image id is kept in
`<data_dir>/modal/images.json` and in the `envs.image_id` column, and reused through the lazy
`modal.Image.from_id(id)`. That object is never hydrated by hand (Modal refuses
`Image.from_id(...).hydrate()`): it goes straight into `Sandbox.create`, which raises
`NotFoundError` for an id that no longer exists. The executor then rebuilds the image once and
remembers the new id.

Every build first passes the daily budget check (with two minutes at the limit rate as headroom)
and the daily build cap, `COVERSET_MAX_IMAGE_BUILDS_PER_DAY` (20); its wall time is then recorded as
`executor_spend` (kind `build`) at the shard limit rate. Modal's billing of image builds is not
verified from the client, so that figure is an estimate. Builds are not sandboxes and take no
`COVERSET_MAX_SANDBOXES` slot; one build per environment runs at a time in a process.

**Untrusted pull requests never build an environment.** The image builder installs the
requirements with network access, and the requirements come from the PR's `uv.lock`. So an
untrusted run whose `env_hash` has no image yet is refused during planning (`skipped`, `neutral`):
without approval with the reason "untrusted PR changes the environment: approve to build"; after a
maintainer's approval it builds only if no requirement is a direct reference (`pkg @ <url>`,
`git+`, `hg+`, `svn+`, `bzr+`, `file:`, any URL, or a pip option such as `--index-url` or `-e`;
`env_export.direct_references`), and is refused otherwise. An untrusted PR that keeps the
lockfile reuses the image trusted runs built. Trusted runs build as before.

### Trusted snapshots: one per default-branch commit

`coverset warm` and the runner (after a trusted baseline of the default branch's tip completes with
verdict `success` or `failure`; see [orchestrator.md](orchestrator.md#how-a-run-flows), step 8) call
`ModalExecutor.ensure_snapshot`, which reuses a ready, unexpired snapshot of the same commit and
environment and otherwise builds one, only for a commit reachable from the repository's default
branch; a PR head is refused. A sandbox from the env image receives
the commit as a `git archive` tarball. When the environment needs an install step
(`install_needed(spec)`: `[tool.coverset] install` is `wheel` or `editable`, or the requirements carry
`# coverset-local:` lines), it then runs, with the network blocked and before collection,
`python -m pip install --no-deps --no-build-isolation --no-index` for the local packages first
(regular ones as `name @ file:///work/repo/<path>` requirements, so their markers pick the right
wheel; editable ones with `-e`), then for the project itself (`-e` for `editable`), and finally
coverset's import-fallback writer for the source root (`snapshot_install_commands`; a failure fails
the snapshot build, and runs keep the previous snapshot or the plain env image). Then it runs
`python -m pytest --collect-only -q -p no:cacheprovider` and `python -m compileall
--invalidation-mode checked-hash` into `PYTHONPYCACHEPREFIX=/work/.pycache` (hash-checked bytecode
survives the next checkout: unchanged files reuse it whatever their mtime), then
`snapshot_filesystem(ttl=7 days)`. The installed copy is the default branch's: `PYTHONPATH` still
puts a run's own checkout first, so a PR's pure-Python changes are seen, while distribution metadata
and compiled extensions come from the snapshot's build until the next one. A Modal run without a
ready snapshot (on the plain env image) has no installed project; the LocalExecutor, which has no
snapshots, installs it per commit instead (`LocalExecutor.install_project`). The image id goes into `snapshots` with `expires_at` one hour
before Modal drops it. The planner uses the newest ready snapshot for the run's environment and
never waits for one. A run on a snapshot still receives its own source: `put_tree` removes the
snapshot's `repo/` before extracting, so files deleted since the snapshot cannot linger.

### Workers

A worker is one sandbox; its root is `/work` (`repo/` the checkout, `run/` the shard's files). Source
arrives by `filesystem.write_bytes` of the tarball plus `tar -xzf` in the sandbox; files move with
`filesystem.read_bytes/write_bytes`. Every command runs under `env -i` with a fixed base environment
(`PATH`, `HOME` and `TMPDIR` under `/work`, `PYTHONUNBUFFERED`, `PYTHONPYCACHEPREFIX`, `LANG`, `TZ`) plus
the sanitised variables the orchestrator passes (`PYTHONPATH`, the event nonce, the run's
`extra_env`), so nothing from the container's own environment leaks into a test. Stdout and stderr
are read concurrently, split into lines (cut at 1 MiB), and capped at `COVERSET_MAX_STDOUT_BYTES`.
Each exec carries a unique `COVERSET_EXEC_ID`; Modal's process handle has no kill, so `cancel()`
and timeouts run a tiny helper that sends SIGTERM to every process with that id in
`/proc/<pid>/environ` (children included), then SIGKILL after 5 s. SIGTERM first matters: the
plugin writes its bundle when it receives it. `release()` terminates the sandbox.

### The warm pool

`ModalExecutor.fill_pool(snapshot, repo, size, ttl)` pre-creates up to `COVERSET_POOL_SIZE` (4)
sandboxes from the trusted snapshot with role `pool`. They are never exec'd before a run claims
them; `acquire` claims matching ones first (same repo, image, network policy and limits, still
running), retags them with the run, and creates the rest. A claimed sandbox serves exactly one run
and is terminated afterwards. Unclaimed ones expire after `COVERSET_POOL_TTL_S` (600 s): the
executor drops them and Modal's `idle_timeout` terminates them on its side even if the control
plane is gone (with `COVERSET_POOL_TTL_S=0` there is no `idle_timeout`, and only `timeout` ends
them). A pooled sandbox is created with `timeout = COVERSET_RUN_TIMEOUT_S + COVERSET_POOL_TTL_S`, so
one claimed at the end of its idle time still lives through the run's full timeout. A new snapshot
replaces the pool: a fill terminates pooled sandboxes of any other image before topping up. Idle
time is recorded as `executor_spend` (kind `pool`) when a pooled sandbox is claimed or terminated.

`coverset serve` keeps the pool filled for the Modal executor (`app.schedule_executor_upkeep`, not
started under `COVERSET_ENV=test`): from startup when `COVERSET_EXECUTOR=modal`, and otherwise from
the moment the serving process first builds a Modal executor, for example to execute a run queued
by `coverset run --no-wait --executor modal`; the reaper below starts the same way. Every 30 s its
`PoolMaintainer` (`orchestrator/pool.py`) looks
for repos with a Modal run created in the last `COVERSET_POOL_TTL_S` seconds and tops each one's pool
up to `COVERSET_POOL_SIZE` from that repo's newest ready, unexpired trusted snapshot; a repo without
one gets no pool, and the pool of a repo that went quiet is drained. After a re-snapshot the runner
refills that repo's pool at once. While today's spend has reached `COVERSET_MODAL_DAILY_BUDGET_USD`
nothing is filled and every pool is drained; fills also go through `fill_pool`'s own budget check and
take slots from the `COVERSET_MAX_SANDBOXES` counter like any other sandbox. When `serve` stops, it
cancels snapshot builds in flight, drains the pool and closes the executor, so nothing it created
keeps running. `COVERSET_POOL_SIZE=0` turns the pool off. `coverset run` and `coverset demo
--no-serve` keep no pool: their runs create sandboxes on demand (the benchmark fills a pool itself).

## Cost guards (every one enforced in code)

| guard | config key (default) | where |
| --- | --- | --- |
| CPU request and limit per sandbox: `cpu=(0.125, limit)` | `COVERSET_SHARD_CPU_LIMIT` (2.0) | `sandbox_options` |
| memory request and limit: `memory=(512, limit)` MiB | `COVERSET_SHARD_MEMORY_MIB` (2048) | `sandbox_options` |
| sandbox lifetime `timeout` never above the run timeout (pooled sandboxes: run timeout + pool TTL) | `COVERSET_RUN_TIMEOUT_S` (1800, at most 7200) | `sandbox_options` |
| `idle_timeout` on every sandbox (unclaimed pool, abandoned workers); none when the TTL is 0 | `COVERSET_POOL_TTL_S` (600, at most 86400) | `sandbox_options` |
| one counter per process over the pool, shards and snapshot builds it creates (two processes, e.g. `serve` and a CLI `coverset run`, each have their own) | `COVERSET_MAX_SANDBOXES` (48, at most 256) | `SandboxSlots` |
| shards per run, `coverset run --shards N` included | `COVERSET_MAX_SHARDS` (32, at most 128) | planner |
| per-run cost cap: the planned run reserves `min(cap, workers x run timeout at the limit rate)` and is admitted only if that fits the daily budget; a run whose held workers pass the cap is stopped (STOP files, cancel) and ends `error`, `neutral`, "run cost cap $X reached" | `COVERSET_MAX_RUN_COST_USD` (1.00, at most the daily budget) | runner, `budget.reserve`, `ModalExecutor._guard_budget` |
| env image builds per UTC day | `COVERSET_MAX_IMAGE_BUILDS_PER_DAY` (20) | `ModalExecutor._build_env` |
| CPU and memory ceilings | `COVERSET_SHARD_CPU_LIMIT` at most 16, `COVERSET_SHARD_MEMORY_MIB` at most 65536, `COVERSET_POOL_SIZE` at most 32 | `config.py` |
| concurrent runs | `COVERSET_MAX_CONCURRENT_RUNS` (4) | queue |
| retries per run | `COVERSET_RETRY_BUDGET_PER_RUN` (5) | runner |
| pool size per active repo (repos with a run in the last `COVERSET_POOL_TTL_S`); 0 disables the pool | `COVERSET_POOL_SIZE` (4) | `fill_pool`, `PoolMaintainer` |
| pool maintenance interval and reaper interval while serving | 30 s and 60 s (constants `POOL_INTERVAL_S`, `REAP_INTERVAL_S`) | `orchestrator/pool.py`, `orchestrator/reaper.py` |
| daily budget: a run is admitted, and a sandbox or image build started, only if today's spend plus what it may cost fits; a refused run concludes `neutral`, "budget exhausted" | `COVERSET_MODAL_DAILY_BUDGET_USD` (5.00, at most 1000) | runner policy, `budget.reserve` and `ModalExecutor._guard_budget` |
| kill switch: webhooks stored, no runs start | `COVERSET_PAUSED` (false) | queue |
| network: `block_network=True` unless the policy says otherwise; `allowlist` uses `outbound_domain_allowlist`; untrusted PRs' sandboxes are always blocked. Image builds are not sandboxes and have network, so an untrusted PR never builds one without approval, nor with direct references (see Images) | `COVERSET_SANDBOX_NETWORK` (blocked), `COVERSET_SANDBOX_DOMAIN_ALLOWLIST` | `sandbox_options`, planner |
| no secrets and no OIDC token in sandboxes: `secrets=[]`, `include_oidc_identity_token=False` | (always) | `sandbox_options` |
| every sandbox tagged `{coverset_instance, repo, run, role, created, owner}` (`created` in UNIX seconds, `owner` = `<host>:<pid>`), never named | (always) | `_create_one` |

**What "today's spend" is.** One sum, the same in every process that shares the database and across
restarts (`orchestrator/budget.py`, `spent_today`): today's (UTC) `runs.cost_estimate` (finished
runs' costs and in-flight runs' reservations) plus today's `executor_spend` rows (pool idle time,
snapshot builds, smoke tests, benchmarks, image builds: spend that belongs to no run, written when
the sandbox is claimed or terminated or the build ends; `coverset modal smoke` records its
`debian_slim` image build as kind `build`, priced like an env image build, and runs under the same
SIGTERM handling as `coverset run`, so its sandbox is terminated when the command is stopped). The executor's own check adds what only
it knows: the accrued time of the non-shard sandboxes it holds right now and rows not yet written.
`PoolMaintainer` and `budget.reserve` read the database sum. A new day starts from zero.

**Rate card and estimates.** Modal bills `max(request, usage)` per second: $0.142 per core-hour and
$0.024 per GiB-hour (2026 prices). Usage is not visible to the client, so the executor prices a
running shard at its limits, `cost_usd = seconds * (cpu_limit * 0.142 + memory_limit_GiB * 0.024) /
3600`, which is conservative, and an idle pooled sandbox at its requests, `seconds * (0.125 * 0.142 +
0.5 * 0.024) / 3600` ($0.03 per sandbox-hour). `runs.cost_estimate` sums `cost_usd` over the time
the run held each worker, from acquire to release.

## The reaper and cleanup

Each installation has an id, a UUID created once as the `instance_id` row of the database's
`settings` table (the first executor that needs it inserts a proposal with `INSERT ... ON CONFLICT
DO NOTHING` and every process reads the winner back; `executors/modal_.py`, `instance_id`), and tags
every sandbox with it. Every process and container on the same database shares it, so a restarted
or replaced container, and `coverset modal reap` run anywhere against that database, find the
sandboxes an earlier one left behind. `coverset modal reap` lists the App's sandboxes with that tag
(`Sandbox.list(app_id=..., tags={"coverset_instance": ...})`) and terminates every one whose `run`
tag is not a queued or running run, except two kinds it keeps: the sandboxes its own process holds,
and pool, snapshot, smoke and benchmark sandboxes younger than their natural lifetime (from their
`created` tag: `COVERSET_POOL_TTL_S` + 60 s for the pool, `COVERSET_RUN_TIMEOUT_S` for the others),
because another process sharing the database may own them: a `serve`'s pool, a `coverset warm`
snapshot build, a `coverset modal smoke`. So a CLI reap never kills a running server's pool, and
`serve`'s reaper never kills a concurrent `warm` or `smoke`. `--all` ignores both exceptions and
terminates every sandbox of the installation, active runs included. The instance id is per
database, so sandboxes of other installations (the benchmark's, a scratch demo's) are invisible to a
plain reap; `--any-instance` looks at every sandbox in the App that has a `coverset_instance` tag instead,
applies the same rules, and therefore terminates those installations' shards (their runs are not in
this database). `coverset serve` runs the plain pass (`orchestrator.reaper.reap_forever`) when its
Modal executor starts and then every 60 s. Run
`coverset modal reap` by hand (or from cron) when no `serve` is running, for example after
`coverset run` or `coverset demo --no-serve` processes that were killed. Every sandbox also carries `timeout` and `idle_timeout`, so
Modal ends a forgotten one on its own within `COVERSET_RUN_TIMEOUT_S` (a pooled one: plus
`COVERSET_POOL_TTL_S`) at the latest. `terminate()` is asynchronous: a terminated
sandbox can stay listed for a few seconds.

Images and snapshots are not sandboxes. Snapshot images expire on Modal's side after their TTL (7
days). Env images do not, and Modal has no API to delete an image: `coverset modal images` lists the
ones built through this data dir (it reads `<data_dir>/modal/images.json`; other data dirs keep
their own) with their last use and flags those unused for 30 days for manual deletion from the
dashboard. The executor creates nothing else: no Secrets, no Volumes, no deployed functions (only the
empty `coverset-runner` app record). Serving the control plane itself from Modal is separate and
opt-in: [Serving the control plane on Modal](#serving-the-control-plane-on-modal).

To check by hand that nothing is left running:

```sh
uv run coverset modal reap --all --any-instance
uv run python -c "import modal; app = modal.App.lookup('coverset-runner', environment_name='coverset'); print([s.object_id for s in modal.Sandbox.list(app_id=app.app_id) if s.poll() is None])"
```

## The alternative: Functions with memory snapshots (designed, not built)

Sandboxes were chosen because they are the closest thing to a CI runner: arbitrary commands,
per-run filesystems, network policy per sandbox, created and destroyed by the control plane with no
deployment. Modal Functions with memory snapshots are the cheaper path for steady load: a deployed
`@app.function(enable_memory_snapshot=True)` whose snapshot is taken after importing the target's
dependencies would start a shard with the interpreter and imports already warm, and Functions bill
about a third of the Sandbox rate. The design: one Function per `env_hash`, deployed by `coverset
warm` (image as above), taking `(tarball, args, scores, nonce)` and returning the bundle bytes, with
streaming over a `modal.Queue` per run; the snapshot is taken after importing the project's
third-party packages but never the project's own code, so a PR cannot poison it. It was not built
because it needs a deployment per environment (against "no functions are deployed"), its
cancellation semantics (SIGTERM so the plugin writes the bundle) differ, and Sandbox memory
snapshots, the other route, are alpha and destructive.

## Live verification

`uv run coverset modal smoke` on 2026-10-11 (modal 1.6.1, environment `coverset`):

```
token               0.14 s  workspace boydgreenfield, user boydgreenfield, token ak-...
app lookup          0.06 s  ap-4lTE77hkl8qfOrQsuvRmGC (coverset/coverset-runner)
image build         0.11 s  im-R5Wufnqy0S6jjwIcH8Q8fV
sandbox create      0.18 s  sb-01M4NHZA17MX11ZESG0MNQ5S1K (network blocked)
exec python -V      0.59 s  Python 3.13.3
write+read file     0.32 s  26 bytes round-tripped
SIGTERM an exec     2.12 s  stopped 0.12 s after cancel()
terminate           0.04 s  sandbox terminated
modal smoke: ok
```

`COVERSET_DATA_DIR=<scratch> uv run coverset demo --executor modal --no-serve` reproduced the local
demo's verdicts: six `main` baselines PASS (main-4 with its deliberately flaky test recorded as
flaky), PR 1 FAIL with `test_refund_partial` confirmed at rank 1 (66th in file order, first failure
5.95 s after the run was queued), PR 2 PASS with gaps. The eight runs recorded $0.007 in
`runs.cost_estimate` (priced on exec seconds at the time; the runner now prices held worker time).

## Measured numbers

`uv run python scripts/bench_modal.py --max-cost-usd 2` on 2026-10-11 (modal 1.6.1, region
us-central, Starter plan, Python 3.13, every sandbox 0.125-2.0 CPU and 512-2048 MiB). Medians of 5
(p90 in brackets; the cold image build is n = 1). Full table, raw values and commands:
[benchmarks.md](benchmarks.md); the keys are in `docs/numbers.json`. The five replayed-webhook rows
come from one later session of 5 trials on the same day: the second pass, after the demo SHAs and the
file-order estimate changed, running the `webhook` scenario on its own after
`coverset demo --executor modal --no-serve` (see [Webhook to verdict](benchmarks.md#webhook-to-verdict)).

| what | seconds |
| --- | ---: |
| env image build, cold (`MODAL_IGNORE_CACHE=1`, n = 1) | 70.8 |
| env image build, cached | 0.46 (0.50) |
| create from the env image, then the first exec | 1.16 (2.16) |
| create from the trusted snapshot, then the first exec | 1.02 (1.42) |
| exec on a pooled sandbox idle for 10 s | 0.32 (0.40) |
| trusted snapshot build (upload, collect, compile, snapshot; n = 1) | 4.0 |
| replayed webhook to first test, pooled sandboxes (demo refunds PR rerun) | 5.54 (6.41) |
| replayed webhook to all workers holding the source | 1.51 (2.73) |
| replayed webhook to the failing test's first result | 5.98 (6.57) |
| replayed webhook to the first confirmed failure (after its retry) and the verdict | 7.33 (7.99) |
| that run's file-order estimate, from the first test | 7.9 (8.88) |
| bench suite (1,200 synthetic tests), 1 shard: start to last test | 181.1 (188.4) |
| the same across 16 shards | 19.6 (21.2) |
| the same across 32 shards | 13.2 (14.4) |
| measured cold path, bench suite: fresh sandbox, install, plain pytest | 179.3 (190.6) |
| measured cold path, demo refunds PR: failing test reported | 6.4 (7.8) |

Costs, priced at the limits over each sandbox's lifetime (the recorded `runs.cost_estimate`, which also spans the terminate calls, comes out slightly higher): $0.017 per 1-shard,
$0.036 per 16-shard and $0.061 per 32-shard bench-suite run. No capacity errors up to 32 shards.
The whole benchmark was estimated at $0.70 the same way.

Two caveats. The bench suite's `busy()` calibrates itself at import time under whatever tracer is
active, so its tests take their drawn durations with or without instrumentation: it measures fan-out,
not coverage overhead (the 1-shard coverset run and the plain cold path both take about 180 s for
that reason). And the demo app is small: on the refunds PR the cold path reports the failure 6.4 s
after the sandbox is requested; coverset reports the failed result 6.0 s after the webhook arrives
and confirms it (failure plus retry) at 7.3 s, with planning, collection and the retry included,
while file order on the same sandbox would reach the test after an estimated 7.9 s. With a suite this
small, start-up dominates; the bench-suite rows show where the time goes on a larger one.

## Serving the control plane on Modal

The executor above needs no deployment, but the control plane can live on Modal too:
`deploy/modal/coverset_web.py` defines the App `coverset-web` (environment `coverset`) with one
`@modal.asgi_app` function, `web`, that returns `create_app()`. It runs one always-on container by
default (`min_containers=1`, `max_containers` from `COVERSET_WEB_MAX_CONTAINERS`,
`@modal.concurrent(max_inputs=256)`, 24 h per-request timeout, 0.5 core and 1 GiB reserved: about
$0.76 a day) and keeps no state in it: the Secret `coverset-web` names Postgres
(`COVERSET_DATABASE_URL`), a bucket (`COVERSET_BLOB_URL` with `AWS_ACCESS_KEY_ID`/
`AWS_SECRET_ACCESS_KEY`), the admin token and `COVERSET_SECRET_KEY`, and a container whose Secret
lacks any of them refuses to start, naming it. The data dir and the cache are on the container's own
disk. The Secret carries no GitHub App keys: the App `/setup` creates is stored in the database,
encrypted under `COVERSET_SECRET_KEY` ([security.md](security.md#5-secrets)), and every container
picks it up within seconds. An operator who prefers to manage the App's keys in the Secret adds
`COVERSET_GITHUB_APP_ID`, `..._CLIENT_ID`, `..._CLIENT_SECRET`, `COVERSET_GITHUB_APP_PRIVATE_KEY`
(the PEM text) and `COVERSET_GITHUB_WEBHOOK_SECRET` instead; `/setup` is then read-only. From inside that
container the Modal client is authenticated by the runtime, so the dispatcher creates shard
sandboxes in `coverset-runner` with no token stored anywhere; a run queued there on 2026-10-13 built
its env image and passed 97 tests.

```sh
uv run modal secret create coverset-web --from-dotenv <file> -e coverset   # ADMIN_TOKEN, SECRET_KEY, PUBLIC_URL, DATABASE_URL, BLOB_URL, AWS keys
uv run modal deploy --strategy recreate -e coverset deploy/modal/coverset_web.py
uv run modal app logs coverset-web -e coverset
uv run modal app stop coverset-web -e coverset
```

What was checked against the platform (details and numbers in [deploy.md](deploy.md)):

* **ASGI lifespan works.** Modal runs the lifespan when the container starts, so Quart's
  `before_serving` hooks migrate the database and start the dispatcher, gc, reaper and warm-pool
  maintainer; `after_serving` runs when the container stops (the pool is drained).
* **The 150 s limit does not cut streams.** Modal documents a 150 s maximum for web requests (a
  303 redirect after that); streamed responses are not covered by it in practice: SSE streams of
  210 s and 330 s (with 110 s of silence) and a 216 s coverset run-page stream completed with HTTP
  200. The earlier reason not to serve the UI from Modal is gone.
* **Client address.** Modal's runtime puts the caller's address and `https` in the ASGI scope and
  drops client-sent `X-Forwarded-For`, so `COVERSET_TRUST_PROXY` stays `false` there.
* **Deploy with `--strategy recreate`** while `max_containers` is 1: a rolling deploy waits forever
  for a second container slot that one container never grants.

