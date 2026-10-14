# Morning runbook

What to do, in order, to take coverset from this branch to running on real repositories. Every command runs from the `coverset/` directory of the checkout unless it says otherwise. Each step says what it costs and what to look at when it fails.

## 0. Where things stand

- Branch `boyd/testing` holds everything, pushed to `origin`. The round-5 changes are in one checkpoint commit ("Round 5: ...") plus the integration commits after it; `git log --oneline -15` lists them.
- A control plane is already **running on Modal**: `https://boydgreenfield-coverset--coverset-web.us-east.modal.run` (App `coverset-web`, environment `coverset`, one always-on container, data on the Volume `coverset-data`, secrets in the Secret `coverset-web`). It costs about $0.76 a day while idle. The admin token is in `~/.config/coverset/modal-web-admin-token` (mode 600; it is not in the repository and was rotated once overnight). The deployment runs the code that was on disk when it was built; redeploy from the committed branch before pointing a GitHub App at it (step 2).
- The deployment already holds a test repository (`local/ledger2`) with a 97-test run that executed in Modal sandboxes from inside the web container, so the executor path works from there without a Modal token.
- Locally, `uv run coverset dev` seeds the demo and serves with reload; `make check` is the fast gate and `make test` the whole suite. `docs/dev.md` is the developer guide.
- The test suite: `uv run pytest -q -n 6 -p no:cacheprovider` (the whole control plane), `make test-plugin-312` (the plugin on Python 3.12), `make test-vitest` (the Vitest reporter).

```sh
curl https://boydgreenfield-coverset--coverset-web.us-east.modal.run/healthz
uv run modal app logs coverset-web -e coverset -f           # live logs
uv run modal app stop coverset-web -e coverset              # stop it (the Volume and Secret stay; deploy again to resume)
```

## 1. Choose where it runs

| | Modal (running now) | Fly.io | A VM (AWS Lightsail or EC2) |
|---|---|---|---|
| Set-up | done | `deploy/fly/README.md`: `fly launch --no-deploy`, a volume, secrets, `fly deploy` | `deploy/aws/README.md`: `user-data.sh` installs Docker and Caddy, runs `deploy/compose.yml` |
| Cost | about $0.76/day | about $8/month | about $12/month (Lightsail) |
| Persistence | SQLite on the container's disk, backed up to the Volume every 60 s when changed and on shutdown; facts and mirrors on the Volume | a volume at `/data` | an EBS volume at `/data` |
| Recovery point on a crash | 60 s | none (local disk) | none (local disk) |
| Modal token for the executor | not needed | `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` | the same |
| Verified overnight | live: health, sign-in, a replayed webhook, a 97-test run in sandboxes, SSE streams of 210 s and 330 s | not deployed (no account); `fly.toml` checked against the docs | not deployed (no AWS access); `user-data.sh` dry-run in an Ubuntu container |

Keep Modal for today. Two rules learned the hard way: redeploy with `--strategy recreate` (a rolling deploy deadlocks with one container, which cost 25 minutes of downtime overnight), and deploy from a clean checkout (the Modal client refuses a build whose files change mid-upload).

```sh
uv run modal deploy --strategy recreate -e coverset deploy/modal/coverset_web.py   # about 23 s of downtime after the command returns
```

`docs/deploy.md` has the measurements, proxy and cookie rules, backups, secret rotation and a troubleshooting list.

## 2. Create the GitHub App

Everything below happens in the browser against the deployed URL. No GitHub object was created overnight; this is the first one.

1. Redeploy from the committed branch (the command above) so the deployment has tonight's fixes.
2. Open `<url>/login` and sign in with the admin token.
3. Open `<url>/setup`. For an organization's repositories use `<url>/setup?org=<org>`; for your own repositories use the plain page. Press **Create the GitHub App**. GitHub shows the manifest (private App, minimal permissions: checks write, contents read, pull requests write, issues write, metadata read; events: pull_request, push, check_suite, check_run, installation; the webhook URL, the setup callback and the sign-in callback are all derived from `COVERSET_PUBLIC_URL`). Confirm.
4. GitHub redirects back; coverset writes the App id, client id, client secret and webhook secret to `/data/.env` and the private key to `/data/secrets/github-app.pem` on the Volume, and sets `COVERSET_ALLOWED_OWNERS` to the App's owner when none is configured.
5. Restart so the settings are re-read: `uv run modal container list -e coverset`, then `uv run modal container stop -y <id>` (14 s), or the redeploy command.
6. Install the App on the repositories you want (the success page links to the installation page). Add more owners later by adding `COVERSET_ALLOWED_OWNERS=a,b` to `/data/.env` on the Volume or to the Secret (`modal secret create coverset-web --force --from-dotenv <file> -e coverset`, keeping the existing admin token and secret key), then restart.
7. Check: `uv run modal container exec --no-pty <id> -- sh -c 'cd /data && coverset deploy check'`. Every row should be PASS once the App exists.

Then sign in with GitHub: set `COVERSET_ADMINS=<your GitHub login>` in the Secret (or `/data/.env`), restart, open `<url>/login` in a private window, press **Sign in with GitHub**. `/account` lists your repositories with their permissions; `/audit` shows the log. Collaborators sign in the same way and see exactly what GitHub lets them see. `docs/auth.md` has the details; `docs/github.md` the webhook side.

## 3. The first private repository

Two routes. Route A gets results in ten minutes and needs no runners; Route B is the anticipatory mode.

**Route A: upload from the repository's own CI.** In the repository's workflow:

```yaml
permissions: { contents: read, id-token: write }
steps:
  - uses: actions/checkout@v4
    with: { ref: ${{ github.event.pull_request.head.sha || github.sha }} }
  # install the project's test dependencies as the job already does, then:
  - run: pip install "git+https://github.com/<you>/machine-musings@boyd/testing#subdirectory=coverset/packages/pytest-coverset"
  - run: python -m pytest --coverset
  - if: always()
    run: coverset-ci upload --server https://boydgreenfield-coverset--coverset-web.us-east.modal.run --with-sources
```

The first OIDC upload registers the repository (its owner must be in `COVERSET_ALLOWED_OWNERS`). The job log prints the run URL. With the App installed, coverset fetches the repository and the PR page shows patch coverage; without it the run still gets totals, gaps and invariants. Add `[tool.coverset] source = ["<package>"]` to the repository's `pyproject.toml` (or pass `--coverset-source`). A matrix job gives each leg its own `--label` and sets `--shards N`. The composite action `src/coverset/forge/gha/upload` wraps the three steps, and `docs/examples/coverset-upload.yml` is a complete workflow. Without OIDC, a repository token works: `coverset users tokens create --repo OWNER/NAME --scopes upload --name ci` (run inside the container or against the same data directory), stored as a repository secret and passed as `COVERSET_TOKEN`. `docs/upload.md`.

**Route B: managed runs on Modal.** First `uv run coverset doctor --repo <path to a clone>` and fix every `[x]` finding (it prints the environment source, packages, test paths, install mode and the exact commands; `--write-config --config-out file.toml` suggests a `[tool.coverset]`). Commit the section. Then, in the container, `coverset warm --repo OWNER/NAME` builds the environment image and the first trusted snapshot (where projects that need installing get installed). Open a pull request: the `pull_request` webhook queues a run, tests start in sandboxes, and the PR gets the check run and the sticky comment. Policy for forks: `/settings/instance` (or `coverset settings set policy.untrusted_pr_policy run_blocked_network`), and `policy.max_shards 8` to bound cost. `docs/getting-started.md`.

The repository you tested last week (`mainline`): the doctor reports it ready except for its two committed wheels stored in Git LFS, which the bare mirror exports as pointers; Route A does not have that problem (the job's own checkout has the files), Route B needs the wheels on a private index (`UV_EXTRA_INDEX_URL` in the control plane's environment) until the mirror fetches LFS objects.

## 4. Mirrors of busier public repositories

Fork the repository, install the App on the fork, keep the fork's owner in `COVERSET_ALLOWED_OWNERS`, push a branch and open a pull request inside the fork (PRs into a repository you do not own get no OIDC token and no webhook). Candidates and their environment source, as the doctor will report them:

| Repository | Environment | Notes |
|---|---|---|
| `pydantic/pydantic` | `uv.lock` (dev group) | pytest, `testpaths = ["tests"]`, Python 3.10+; a few thousand tests; needs `pydantic-core` wheels, which the lock pins |
| `encode/httpx` | requirements file | pytest with `filterwarnings = error`; small and fast |
| `pallets/flask` or `pallets/click` | `pyproject` + requirements files | pytest, src layout |
| `psf/requests` | `setup.py` + `requirements-dev.txt` | read statically; the doctor says whether it needs `install = "wheel"` |

Cost guards for the Modal executor: `COVERSET_MODAL_DAILY_BUDGET_USD` (5.00) and `COVERSET_MAX_RUN_COST_USD` (1.00) in the Secret; raise them for a 5,000-test suite. `coverset modal reap --all` terminates stray sandboxes.

## 5. Settings worth setting on day one

- `/settings/instance`: untrusted pull requests (`approve` in App mode, `run_blocked_network` for forks you control), `policy.max_shards`, `policy.ai_review` (off unless `COVERSET_AI=anthropic` and a key), `policy.test_tax_threshold` (8).
- `/repos/<id>/settings`: per repository overrides; `coverset settings list --repo OWNER/NAME` prints each value with its source.
- `/account/settings`: your view preferences (highlight only the misses, map module weight, theme). The same keys answer to `?highlight=`, `?modules=` and the keys `h`, `m`, `M`.
- `/settings/tokens`: API tokens (shown once). A read-scope token is what the MCP server uses against the deployment: `coverset mcp install-claude-code --server <url>`.

## 6. When something breaks

| Symptom | Look at |
|---|---|
| The webhook answers 503 | the webhook secret is empty: `/setup` did not finish, or the container was not restarted |
| The webhook answers 202 and nothing runs | `/forge/outbox`, the run list on `/`, the dispatcher's log lines; `COVERSET_PAUSED`; the owner is not in `COVERSET_ALLOWED_OWNERS` (stored as `ignored_owner`) |
| An upload answers 401 or 403 | the token's repository or scope; for OIDC the `--server` URL must equal `COVERSET_PUBLIC_URL` exactly (it is the audience) |
| A run says "budget exhausted" | the daily budget in the Secret; `coverset modal reap --all` after an interrupted run |
| Pages are slow after an ingest | the first hit after a new run is cold (about 1 s); the caches are bounded by `COVERSET_RUN_FACTS_MB` and `COVERSET_FACTS_CACHE_ENTRIES` |
| Sign-in fails with "state" errors | the callback URL on the App must be `<public url>/auth/github/callback`; the manifest sets it for new Apps |
| Anything else | `coverset deploy check`, `/audit`, `uv run modal app logs coverset-web -e coverset`, `docs/deploy.md` troubleshooting |

## 7. What landed overnight

Twelve features, each reviewed and fixed, then three integration passes (cross-feature wiring, web polish, the dev loop), the README, ARCHITECTURE and this page. The one-line versions, with their pages:

1. GitHub sign-in, server-side sessions, per-repository access mirrored from GitHub, instance admins, API tokens, an audit log, `coverset users` ([docs/auth.md](auth.md)).
2. Scoped settings at instance, organization, repository and user level, wired into intake, planning and the verdict; token pages; `coverset settings` ([docs/settings.md](settings.md)).
3. Deployment: Dockerfile, compose with Caddy, Fly, AWS, the Modal web deployment that is running, `coverset deploy check` ([docs/deploy.md](deploy.md)).
4. Repositories without `uv.lock`, projects that need installing, `coverset doctor` ([docs/getting-started.md](getting-started.md)).
5. Upload mode: `coverset-ci upload`, `POST /v1/bundles`, the composite action ([docs/upload.md](upload.md)).
6. The test economy: leverage, redundancy candidates, the minimal covering set, families, assertion-free tests, direct vs indirect coverage, unexercised handlers, stale tests, the PR test tax; the Economy page, the PR card, the comment line, the report page, `coverset tests` ([docs/economy.md](economy.md)).
7. The plugin under xdist, measured overhead layers, a faster export; the Vitest reporter's concurrency story ([docs/instrumentation.md](instrumentation.md), [docs/typescript.md](typescript.md)).
8. An MCP server and `/v1/agent` with fifteen tools and three playbooks ([docs/mcp.md](mcp.md)).
9. Rust: `coverset rust run` with cargo-nextest and LLVM into the neutral bundle ([docs/rust.md](rust.md)).
10. UI: module weight and a stable ordered layout for the map, highlight modes, keys on every page, one look for the pages and the report ([docs/ui.md](ui.md)).
11. Performance: caches, per-run summaries, content-hashed static assets, a 10x faster ingest index, an optional compact layout; measured on your 470-file, 5,099-test repository ([docs/benchmarks.md](benchmarks.md)).
12. Production flows: the design, the probe package, the importer, the gap report and archetype clustering ([docs/production-flows.md](production-flows.md)).
13. The developer loop: `coverset dev`, `demo --fresh`, the Makefile, the dev-only sign-in ([docs/dev.md](dev.md)).

Known gaps, so nothing surprises you: no real GitHub object was exercised (sign-in and webhooks are tested against stubs and signed fixtures); the Fly and AWS paths are untested live; Git LFS objects are not fetched into mirrors; `POST /v1/flows` and the production UI are designed only; Rust has no argument shapes or branches; test-economy figures are candidates; the probe is installed from this repository, not PyPI.
