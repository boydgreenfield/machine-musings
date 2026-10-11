# coverset on Modal

coverset runs shards in [Modal Sandboxes](https://modal.com/docs/guide/sandbox), driven entirely
from the control plane with the Modal Python client. Nothing is deployed: the App `coverset-runner`
exists only as the container that sandboxes are created in. Code: `src/coverset/executors/modal_.py`
(executor, workers, pool, reaper, guards) and `src/coverset/executors/modal_images.py` (images).

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
  (`modal.App.lookup(..., create_if_missing=True)`).
* **Image builder version.** Modal workspaces default to Image Builder Version 2023.12, whose
  `debian_slim` images stop at Python 3.12; the demo and most targets want 3.13. The client accepts a
  per-process override, and coverset sets `MODAL_IMAGE_BUILDER_VERSION=2025.06` (Python 3.10 to 3.14)
  before any image is defined (`modal_images.configure_builder`). An explicit value in your
  environment wins. Without it the 3.13 build is refused. You can also raise the workspace default
  in the Modal dashboard, which affects every app in the workspace.
* **CI.** Use a token instead of a profile: `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` and
  `MODAL_ENVIRONMENT=coverset` (or `COVERSET_MODAL_ENVIRONMENT`). Store them as CI secrets. They are
  read by the control plane only: the worker environment is rebuilt from scratch, and `MODAL_*`
  variables never reach a test.
* **Run something.** `uv run coverset demo --executor modal`, `uv run coverset run --repo PATH
  --head main --executor modal`, `uv run coverset warm --repo PATH --executor modal` (env image plus
  trusted snapshot of the default branch).

## How it works

### Images: one per environment

An environment is the target repo's locked third-party requirements (`uv export --frozen
--no-hashes --no-emit-project --all-groups`), the Python version from `[tool.coverset] python`, and
the pytest-coverset wheel; `env_hash` hashes all three. Its image:

```python
modal.Image.debian_slim(python_version=spec.python)
  .apt_install("git")
  .env({"UV_COMPILE_BYTECODE": "1", "PYTHONUNBUFFERED": "1"})
  .uv_pip_install(requirements=[<requirements.txt>])
  .add_local_file(<wheel>, "/opt/wheels/<name>", copy=True)
  .run_commands("pip install /opt/wheels/<name>")
```

Everything goes into the system interpreter; tests import the project through `PYTHONPATH`, the
project itself is never installed. The plugin wheel is installed with its dependencies, because a
target's lockfile need not pin `coverage`. The image id is kept in `<data_dir>/modal/images.json`
and in the `envs.image_id` column, and reused through the lazy `modal.Image.from_id(id)`. That
object is never hydrated by hand (Modal refuses `Image.from_id(...).hydrate()`): it goes straight
into `Sandbox.create`, which raises `NotFoundError` for an id that no longer exists. The executor
then rebuilds the image once and remembers the new id.

### Trusted snapshots: one per default-branch commit

`coverset warm` (and `ModalExecutor.ensure_snapshot`) builds a snapshot only for a commit reachable
from the repository's default branch; a PR head is refused. A sandbox from the env image receives
the commit as a `git archive` tarball, runs `python -m pytest --collect-only -q -p no:cacheprovider`
and `python -m compileall --invalidation-mode checked-hash` into `PYTHONPYCACHEPREFIX=/work/.pycache`
(hash-checked bytecode survives the next checkout: unchanged files reuse it whatever their mtime),
then `snapshot_filesystem(ttl=7 days)`. The image id goes into `snapshots` with `expires_at` one hour
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
plane is gone. `coverset.orchestrator.pool.maintain_pool` is the generic entry point; as of this
phase nothing calls it automatically after a baseline run (the benchmark fills the pool itself), so
runs without a filled pool create their sandboxes on demand.

## Cost guards (every one enforced in code)

| guard | config key (default) | where |
| --- | --- | --- |
| CPU request and limit per sandbox: `cpu=(0.125, limit)` | `COVERSET_SHARD_CPU_LIMIT` (2.0) | `sandbox_options` |
| memory request and limit: `memory=(512, limit)` MiB | `COVERSET_SHARD_MEMORY_MIB` (2048) | `sandbox_options` |
| sandbox lifetime `timeout` never above the run timeout | `COVERSET_RUN_TIMEOUT_S` (1800) | `sandbox_options` |
| `idle_timeout` on every sandbox (unclaimed pool, abandoned workers) | `COVERSET_POOL_TTL_S` (600) | `sandbox_options` |
| one process-wide counter over pool + shards + snapshot builds | `COVERSET_MAX_SANDBOXES` (48) | `SandboxSlots` |
| shards per run | `COVERSET_MAX_SHARDS` (32) | planner |
| concurrent runs | `COVERSET_MAX_CONCURRENT_RUNS` (4) | queue |
| retries per run | `COVERSET_RETRY_BUDGET_PER_RUN` (5) | runner |
| pool size per active repo | `COVERSET_POOL_SIZE` (4) | `fill_pool` |
| daily budget: no new sandboxes once today's spend reaches it; the run concludes `neutral`, "budget exhausted" | `COVERSET_MODAL_DAILY_BUDGET_USD` (5.00) | runner policy and `ModalExecutor._guard_budget` |
| kill switch: webhooks stored, no runs start | `COVERSET_PAUSED` (false) | queue |
| network: `block_network=True` unless the policy says otherwise; `allowlist` uses `outbound_domain_allowlist`; untrusted PRs are always blocked | `COVERSET_SANDBOX_NETWORK` (blocked), `COVERSET_SANDBOX_DOMAIN_ALLOWLIST` | `sandbox_options` |
| no secrets and no OIDC token in sandboxes: `secrets=[]`, `include_oidc_identity_token=False` | (always) | `sandbox_options` |
| every sandbox tagged `{coverset_instance, repo, run, role}`, never named | (always) | `_create_one` |

The budget check in the executor adds three things to today's `runs.cost_estimate`: the sandboxes
this process has running now, pool idle time and snapshot builds (which belong to no run).

**Rate card and estimates.** Modal bills `max(request, usage)` per second: $0.142 per core-hour and
$0.024 per GiB-hour (2026 prices). Usage is not visible to the client, so the executor prices a
running shard at its limits, `cost_usd = seconds * (cpu_limit * 0.142 + memory_limit_GiB * 0.024) /
3600`, which is conservative, and an idle pooled sandbox at its requests, `seconds * (0.125 * 0.142 +
0.5 * 0.024) / 3600` ($0.03 per sandbox-hour). `runs.cost_estimate` sums `cost_usd` over the time
the run held each worker, from acquire to release.

## The reaper and cleanup

Each installation has an id, a UUID created once in `<data_dir>/instance_id`, and tags every sandbox
with it. `coverset modal reap` lists the App's sandboxes with that tag
(`Sandbox.list(app_id=..., tags={"coverset_instance": ...})`) and terminates every one whose `run`
tag is not a queued or running run; `--all` terminates all of them. A reap keeps the sandboxes its
own process holds (pool, running shards, snapshot builds); a reap from another process (the CLI)
does not know a server's pool and terminates it, and the pool is refilled on the next fill.
`coverset.orchestrator.reaper.reap_forever` runs the pass every 60 s for a long-lived process; as of
this phase `coverset serve` does not start it yet, so run `coverset modal reap` from cron (or wire
`reap_forever` into the app's startup). Every sandbox also carries `timeout` and `idle_timeout`, so
Modal ends a forgotten one on its own within `COVERSET_RUN_TIMEOUT_S` at the latest. `terminate()` is asynchronous: a terminated
sandbox can stay listed for a few seconds.

Images and snapshots are not sandboxes. Snapshot images expire on Modal's side after their TTL (7
days). Env images do not, and Modal has no API to delete an image: `coverset modal images` lists the
ones this installation built with their last use and flags those unused for 30 days for manual
deletion from the dashboard. Nothing else is created: no Secrets, no Volumes, no deployments.

To check by hand that nothing is left running:

```sh
uv run coverset modal reap --all
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
because it needs a deployment per environment (against "nothing is left deployed"), its
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
[benchmarks.md](benchmarks.md); the keys are in `docs/numbers.json`.

| what | seconds |
| --- | ---: |
| env image build, cold (`MODAL_IGNORE_CACHE=1`, n = 1) | 70.8 |
| env image build, cached | 0.46 (0.50) |
| create from the env image, then the first exec | 1.16 (2.16) |
| create from the trusted snapshot, then the first exec | 1.02 (1.42) |
| exec on a pooled sandbox idle for 10 s | 0.32 (0.40) |
| trusted snapshot build (upload, collect, compile, snapshot; n = 1) | 4.0 |
| replayed webhook to first test, pooled sandboxes (demo refunds PR rerun) | 4.14 (4.63) |
| replayed webhook to all workers holding the source | 0.94 (1.31) |
| replayed webhook to the first confirmed failure (after its retry) | 5.33 (6.37) |
| bench suite (1,200 synthetic tests), 1 shard: start to last test | 181.1 (188.4) |
| the same across 16 shards | 19.6 (21.2) |
| the same across 32 shards | 13.2 (14.4) |
| measured cold path, bench suite: fresh sandbox, install, plain pytest | 179.3 (190.6) |
| measured cold path, demo refunds PR: failing test reported | 6.4 (7.8) |

Costs, priced at the limits over each sandbox's lifetime (an upper bound): $0.017 per 1-shard,
$0.036 per 16-shard and $0.061 per 32-shard bench-suite run. No capacity errors up to 32 shards.
The whole benchmark was estimated at $0.70 the same way.

Two caveats. The bench suite's `busy()` calibrates itself at import time under whatever tracer is
active, so its tests take their drawn durations with or without instrumentation: it measures fan-out,
not coverage overhead (the 1-shard coverset run and the plain cold path both take about 180 s for
that reason). And the demo app is small: on the refunds PR the cold path reports the failure 6.4 s
after the sandbox is requested, coverset confirms it (failure plus retry) 5.3 s after the webhook
arrives, with planning, collection and the retry included; the gap grows with suite size, which is
what the bench-suite rows show.
