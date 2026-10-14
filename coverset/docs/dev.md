# Developing coverset

How to run coverset on your own machine and change it quickly: one command for a seeded server
that reloads, a quick data reset, a sign-in that needs no GitHub App, and the tests to run before
you push. Everything below was run on a MacBook (Apple silicon) on 2026-10-14. The timings are from
that machine.

- [Setup](#setup)
- [The 30-second loop](#the-30-second-loop)
- [Data](#data)
- [Hosted-like development](#hosted-like-development): Postgres + MinIO, `make dev-hosted`
- [A public demo through a tunnel](#a-public-demo-through-a-tunnel): `coverset dev --tunnel`
- [Signing in locally](#signing-in-locally)
- [Exercising runs without GitHub](#exercising-runs-without-github)
- [Iterating on a page](#iterating-on-a-page)
- [Tests](#tests)
- [Debugging](#debugging)
- [Conventions](#conventions)

`make help` lists the shortcuts used on this page. Every recipe prints what it runs.

## Setup

| Tool | For | Check |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | everything Python | `uv sync --all-groups` installs the project, the workspace packages (`pytest-coverset`, `coverset-model`, `coverset-probe`) and the dev group (pytest, pytest-xdist, hypothesis, ruff, playwright) into `.venv` |
| Python 3.13 | the control plane (`requires-python = ">=3.13"`) | uv picks or downloads it |
| Python 3.12 | the pytest plugin's floor (`requires-python = ">=3.12"`) | `make test-plugin-312` runs its tests on 3.12 |
| git | mirrors, the demo repository | |
| node 20+ and npm | the Vitest reporter (`packages/coverset-vitest`) and its end-to-end test | `make test-vitest` |
| docker | optional: `make dev-hosted` (Postgres + MinIO from `deploy/dev-compose.yml`), `deploy/compose.yml`, the Dockerfile | `docker compose version` (2.20+) |
| Modal CLI and a token | optional: `COVERSET_EXECUTOR=modal` and the `modal` tests | |
| cargo (plus `llvm-tools-preview`) | optional: the Rust adapter and its end-to-end test | |

```sh
cd coverset
uv sync --all-groups
make dev            # or: uv run coverset dev
```

## The 30-second loop

`coverset dev` (`make dev`) is the development server:

1. When the data directory holds no repository, it seeds the demo first: the ledger repository,
   six `main` commits and two PRs, run with the local executor, and the `pr_opened` webhook
   replayed. It runs in a separate process, so nothing of it (the local executor's worker pool)
   stays behind in the dev server. That took 17.2 to 17.5 s on an empty directory, once.
   `--no-seed` skips it.
2. It serves on `127.0.0.1:8000` (`--bind` to change it; `--open` opens the browser).
3. It watches `src/coverset` and `packages/*/src`, re-reading the modification times of every
   `.py`, `.html`, `.css` and `.js` file every 0.5 s. Caches, virtualenvs, `node_modules` and
   hidden entries are skipped. A change counts once nothing else has changed for 300 ms, so a
   save-all or a branch switch restarts once.

```
$ COVERSET_DATA_DIR=/tmp/cs uv run coverset dev
coverset dev: data directory /tmp/cs
coverset dev: no repository yet, seeding the demo (about 25 s, once)
...
coverset dev: demo seeded in 17.5 s
coverset dev: watching src/coverset, packages/coverset-model/src, packages/coverset-probe/src, packages/coverset-vitest/src, packages/pytest-coverset/src (every 0.5 s; templates and static files reload without a restart)
coverset dev: serving on http://127.0.0.1:8000 (ready in 0.6 s; Ctrl-C to stop)
coverset dev: src/coverset/web/pages.py changed; restarting the server
coverset dev: ready again in 0.8 s
coverset dev: src/coverset/web/templates/repo.html changed; served fresh, no restart
```

What each kind of edit costs:

| You edit | What happens | Measured |
|---|---|---|
| a template under `web/templates/` | no restart: the server re-reads a changed template on the next request (Quart's `TEMPLATES_AUTO_RELOAD` and Jinja's `auto_reload`, set only by the dev server) | visible on the next request, 0.02 s after saving |
| `app.css`, `app.js`, `features.css` (`web/static/`) | no restart: static URLs carry a hash of the file's contents (`?v=`), recomputed when the file's mtime or size changes, so the next page load asks for the new URL instead of the year-long cached one | next page load |
| any `.py` under `src/coverset` or `packages/*/src` | the server process is replaced: SIGTERM, a 1 s graceful shutdown, a new process | 1.3 to 1.5 s from saving to the new server answering `/healthz` (0.7 to 0.9 s of it is the restart) |
| a migration | not picked up by a reload: stop, `uv run coverset init-db`, start again | |

A Python edit that does not import (a syntax error) stops the server. The reloader then prints
`the server exited with code 1; save a file to restart it`, and the next save starts it again.
Ctrl-C stops both processes. The server runs in its own process session, and the first-run
seeding runs in a process of its own that has exited by then, so worker processes shut down
cleanly instead of each printing a `KeyboardInterrupt` traceback. Checked both ways: Ctrl-C right
after a first run that seeded an empty directory stopped everything in 0.26 s with no traceback
in the output, and so did Ctrl-C on an already seeded one. If the reloader is killed, the server
notices within a second and stops too.

`coverset serve --reload` (`make serve`) is the same reloader without the seeding. Plain
`coverset serve` is unchanged: it does not reload, caches templates, and is what the deployments
run.

The server's own output is the log: one line per request (`hypercorn.access`), the gc pass at
startup, warnings. Alembic's "Context impl SQLiteImpl" lines are gone from every command. Set
`COVERSET_LOG_LEVEL=DEBUG` to get them back. `coverset init-db` prints one line per migration it
applies:

```
$ uv run coverset init-db
applied migration 0001 initial
...
applied migration 0008 accounts, api tokens, repo access, audit log, scoped settings
database ready (8 migration(s) applied): sqlite+aiosqlite:////tmp/cs/coverset.db
$ uv run coverset init-db
database ready (already at the latest revision): sqlite+aiosqlite:////tmp/cs/coverset.db
```

## Data

Everything lives in `COVERSET_DATA_DIR` (default `./data`; directories are 0700, the database
0600), unless the state is hosted ([below](#hosted-like-development)). After the demo and one upload:

| Path | What |
|---|---|
| `coverset.db` (`-wal`, `-shm`) | the control-plane database (SQLite, WAL) |
| `facts/repo=<id>/run=<run_id>/*.parquet` | the immutable per-run facts (Parquet), plus `repo=<id>/dictionary.json` |
| `mirrors/<repo_id>.git` | bare mirrors of each repository |
| `runs/<run_id>/` | per-run scratch: args files, pulled and uploaded bundles |
| `sources/` | source files sent with `coverset-ci upload --with-sources` |
| `demo.git`, `demo.git.shas.json` | the seeded demo repository and its commit SHAs |
| `report/` | the static report of the demo's PR #2 (`coverset demo` builds it; `coverset dev`'s seeding skips it) |
| `secret_key` | the cookie-signing key when `COVERSET_SECRET_KEY` and the admin token are unset |

Start over with `uv run coverset demo --fresh --no-serve` (`make demo-reset`, 19.7 s). It empties the
data directory and rebuilds the demo. It refuses a directory that is neither empty nor holds
`coverset.db`, a symlinked data directory, and a database configured outside the data directory
(`COVERSET_DATABASE_URL`):

```
$ COVERSET_DATA_DIR=~/notes uv run coverset demo --fresh --no-serve
Error: --fresh refuses /Users/you/notes: it is not empty and holds no coverset.db, so it does not look like a coverset data directory (check COVERSET_DATA_DIR)
```

Without `--fresh`, `coverset demo` stays idempotent: running it again adds runs to the same database.

Keep several data directories side by side by pointing `COVERSET_DATA_DIR` at each one. For
example, `COVERSET_DATA_DIR=/tmp/cs-a uv run coverset dev --bind 127.0.0.1:8001` and a second
server on another directory and port.

The facts are plain Parquet: query them through coverset's views, or with DuckDB directly:

```sh
uv run coverset facts query --repo-id 1 --run <run_id> "select count(*) as tests from tests_dim"
uv run python -c "import duckdb; print(duckdb.sql(\"select count(*) from 'data/facts/repo=1/run=<run_id>/tests_dim*.parquet'\").fetchall())"
```

`--run` needs `--repo-id` (`Error: query failed: run_id and run_ids require repo_id` otherwise).

## Hosted-like development

`make dev` keeps everything in SQLite and files, which is what the demo and most work need. A
deployment keeps its state in Postgres and an S3-compatible bucket instead ([deploy.md](deploy.md)),
and some bugs only show up there: a query SQLite forgives, a transaction that holds a lock, a write
that must be conditional, a cache that was never filled. `make dev-hosted` runs the same development
loop against that shape, on your machine.

### The stack

`deploy/dev-compose.yml` is Postgres 17 and MinIO (an S3-compatible server), with throwaway
credentials and ports published on `127.0.0.1` only:

| Service | Where | Credentials |
|---|---|---|
| Postgres 17 | `127.0.0.1:55432`, database `coverset` (`COVERSET_DEV_PG_PORT` moves it) | `coverset` / `coverset` |
| MinIO API | `http://127.0.0.1:9000`, bucket `coverset` (`COVERSET_DEV_S3_PORT`) | `minioadmin` / `minioadmin` |
| MinIO console | `http://127.0.0.1:9001` (`COVERSET_DEV_S3_CONSOLE_PORT`) | the same |

```sh
make services          # docker compose -f deploy/dev-compose.yml up -d --wait (pulls the images the first time)
make dev-hosted        # coverset dev --hosted: starts the services itself when they are down
make services-down     # stop them, keep the data
make services-reset    # stop them and delete both volumes: an empty database and bucket next time
```

Postgres is on 55432 rather than 5432 so that a Postgres already installed on the laptop
(Homebrew, Postgres.app) does not collide with it. `COVERSET_DEV_PG_PORT`, `COVERSET_DEV_S3_PORT`
and `COVERSET_DEV_S3_CONSOLE_PORT` move the three ports; `coverset dev --hosted` asks Docker which
ports the running containers publish (`docker compose port`), so it finds a moved port even when
the variable was set only in the shell that started the stack.

### What `coverset dev --hosted` does

1. Checks the stack with `docker compose ps`; when `postgres` or `minio` is not running it runs
   `docker compose -f deploy/dev-compose.yml up -d --wait` and prints that command. Without docker
   it expects the two ports to answer already (a Postgres and a MinIO you started yourself) and
   says how to start them when they do not.
2. Sets `COVERSET_DATABASE_URL`, `COVERSET_BLOB_URL`, `AWS_ACCESS_KEY_ID` and
   `AWS_SECRET_ACCESS_KEY` to the stack's values in its own environment, which the seeding and
   server children inherit (they replace values from `.env`), and `COVERSET_DATA_DIR` to
   `data/hosted-dev` unless the environment sets one, kept apart from the SQLite demo in `data/`:
   it holds the cache (mirrors, downloaded facts) and the demo's git repository, which the database
   points at (see "Resetting it"). It also removes `AWS_SESSION_TOKEN`, `AWS_SECURITY_TOKEN`,
   `AWS_PROFILE` and `AWS_DEFAULT_PROFILE` from its environment: a session token from an SSO login
   sent with the MinIO keys makes MinIO refuse every request.
3. Prints where everything is, then continues exactly like `coverset dev`: it seeds the demo into the
   Postgres database and the bucket when the database holds no repository, and serves with reload.

```
$ make dev-hosted
coverset dev: the dev services are running (deploy/dev-compose.yml)
coverset dev: hosted-like: Postgres 127.0.0.1:55432/coverset (psql: make services-psql)
coverset dev: hosted-like: object store s3://coverset?endpoint_url=http://127.0.0.1:9000&region=us-east-1
coverset dev: MinIO console http://127.0.0.1:9001 (user minioadmin, password minioadmin; bucket coverset)
coverset dev: data directory .../data/hosted-dev
...
coverset dev: serving on http://127.0.0.1:8000 (ready in 0.7 s; Ctrl-C to stop)
```

(Run on this machine with the stack already up: the migrations of an empty database, then the
server answering `/healthz` and `/` with 200 within a second.)
[`.env.hosted-dev.example`](../.env.hosted-dev.example) holds the same values for a shell
(`set -a; . ./.env.hosted-dev.example; set +a`) or, copied into `.env`, as your default. Any other
Postgres and bucket work the same way: set the four variables and run plain `coverset dev`.

`coverset deploy check --offline` in that environment prints the `database` (Postgres 17, the
migrated revision, the pool), `object store` (the MinIO bucket probed with a conditional write),
`cache dir` and `state` (`hosted`) rows a deployment would.

**What a restart costs** (measured 2026-10-14 on an M4 Max laptop against the dev stack, with
`COVERSET_DATABASE_URL`, `COVERSET_BLOB_URL` and an empty `COVERSET_DATA_DIR` set and `coverset dev
--no-open`): seeding the demo into the empty database and bucket took 20 s and the server answered
23 s after the start. Stopping it, deleting the data directory's `cache/` (every downloaded fact and
the mirror) and starting again: ready in 1-2 s, and every page rendered the same as before the wipe
on its first request, the facts pulled from MinIO and the mirror cloned again on demand.

| Page | first request after the wipe | warm |
|---|---|---|
| dashboard (pulls every baseline for the trend) | 238 ms | 17-30 ms |
| repository overview | 71 ms | 16-19 ms |
| PR #1 (waits for the mirror clone for its diff) | 216 ms | 18-26 ms |
| PR #2 | 134 ms | 19-25 ms |
| source view of `parse.py` | 53 ms | 8-16 ms |
| test page | 74 ms | 15-24 ms |
| economy, map, tests, invariants, run page | 17-34 ms | 9-25 ms |
| run events (SSE; finished run, and replay at 100x) | 25 ms, 44 ms | 21 ms, 46 ms |

Only two things under the data directory outlive the cache in this setup: `demo.git` (the demo's
"upstream", which the database names as the repository's source; a GitHub repository has none) and
`secret_key` (sign-in cookies; a deployment sets `COVERSET_SECRET_KEY` instead).

### Resetting it

`data/hosted-dev` is not all cache: next to `cache/` it holds the demo's git repository, whose
absolute path the database records as the repository's source (mirrors clone from it). It belongs
to the database: reset them together, and never delete the git repository while the database
still names it.

| To | Run |
|---|---|
| start over with an empty database and bucket | `make services-reset && make dev-hosted` (`services-reset` also deletes `data/hosted-dev`; the demo seeds again) |
| empty only the database | `make services-psql`, then `DROP SCHEMA public CASCADE; CREATE SCHEMA public;`, then `rm -rf data/hosted-dev` (a new database numbers repositories from 1 again, so the old cache's `facts/repo=1/` would pass for the new repository's; the bucket's facts are orphans that gc removes) |
| drop the local cache | `rm -rf data/hosted-dev/cache` (the next page view downloads what it needs; leave `demo.git` beside it) |
| remove just the demo repository | `uv run coverset repo purge coverset-demo/ledger --yes` in the hosted environment |

### Looking inside

* **Postgres:** `make services-psql` opens `psql -U coverset coverset` in the container
  (`\dt` lists the tables, `select id, status, verdict from runs order by created_at desc limit 5;`).
  `uv run coverset db status` prints the backend, the revision, the pool and who holds the leader
  lease.
* **MinIO:** the console at `http://127.0.0.1:9001` (minioadmin / minioadmin) browses the `coverset`
  bucket: `facts/repo=<id>/run=<run id>/` holds each run's Parquet files and `MANIFEST.json`,
  `facts/repo=<id>/dictionary.json` the repository's id spaces, `sources/` and `uploads/` what
  `coverset-ci upload` sent. From a shell: `docker compose -f deploy/dev-compose.yml exec minio mc
  ls --recursive dev/coverset` after `mc alias set dev http://localhost:9000 minioadmin minioadmin`.
* **The whole path at once:** `uv run python scripts/smoke_hosted.py` with the hosted environment
  migrates, seeds, serves, fetches four pages, uploads a bundle and lists the run's keys in the
  bucket ([deploy.md](deploy.md#smoke-test-a-hosted-state)); `--clean-only` removes what it made.
  It refuses the `coverset` database that `make dev-hosted` seeded (that demo repository is not the
  smoke's): create a database of its own first, e.g. `make services-psql` then
  `CREATE DATABASE coverset_smoke;`, and point `COVERSET_DATABASE_URL` at `.../coverset_smoke`
  (and `COVERSET_BLOB_URL` at a prefix such as `s3://coverset/smoke?...`).

### The test suite against both

The suite needs no service: SQLite, local files and an in-process moto S3 server. Two variables
point it at the real ones:

| Variable | Effect |
|---|---|
| `COVERSET_TEST_DATABASE_URL=postgresql+asyncpg://coverset:coverset@127.0.0.1:55432/coverset` | the `settings` fixture gets a fresh Postgres database per test, cloned from a template migrated once per worker, and the page and report tests' shared demo is seeded into a Postgres database too; SQLite-specific tests (`sqlite_only`) skip ([postgres.md](postgres.md)) |
| `COVERSET_TEST_BLOB_URL='s3://coverset?endpoint_url=http://127.0.0.1:9000&region=us-east-1'` (with `AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin`) | the object-store tests run against MinIO instead of moto, and the `settings` fixture and the shared demos keep their facts, sources and upload archives under a fresh key prefix there; tests that plant facts in the local layout (`local_files_only`) skip ([object-store.md](object-store.md#testing-against-a-real-store)) |

`make test-pg` runs the whole suite with the first one set against the dev stack; add the second
for MinIO:

```sh
make services
make test-pg
COVERSET_TEST_BLOB_URL='s3://coverset?endpoint_url=http://127.0.0.1:9000&region=us-east-1' \
  AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin make test-pg
```

## A public demo through a tunnel

A demo from your laptop needs a public `https` URL, which a tunnel (ngrok, cloudflared) gives you.
`coverset dev --tunnel` prints what to run and which settings a public URL needs; it never starts
the tunnel itself:

```
$ uv run coverset dev --tunnel
coverset dev: a public demo through a tunnel (coverset does not start it):
coverset dev:   not ready: COVERSET_PUBLIC_URL is http://127.0.0.1:8000, not the tunnel's public https URL
coverset dev:   not ready: COVERSET_ADMIN_TOKEN is unset (a public URL refuses open mode)
  1. start a tunnel to this server in another terminal:
    ngrok http http://127.0.0.1:8000    # prints https://<name>.ngrok-free.app
    cloudflared tunnel --url http://127.0.0.1:8000    # or this: prints https://<name>.trycloudflare.com
  2. set the two settings a public URL needs, in the shell or .env:
    export COVERSET_PUBLIC_URL=https://<the tunnel's host>
    export COVERSET_ADMIN_TOKEN=<64 fresh hex characters>    # 32+ characters; open mode is refused on a public URL
     optional: COVERSET_TRUST_PROXY=true (the tunnel's X-Forwarded-For: rate limits per visitor)
  3. run `coverset dev --tunnel` again, then sign in at <public url>/login with the token
```

With both set it serves as usual and prints the command for the configured host (`ngrok http
--url=https://<name>.ngrok-free.app http://127.0.0.1:8000` for an ngrok domain). Why the two
settings: links, cookies, the OIDC audience and the GitHub App's webhook URL all derive from
`COVERSET_PUBLIC_URL`, and with a public URL open mode would make every visitor the operator, so
settings refuse to load without an admin token (and the development sign-in turns itself off).
Visitors sign in at `<url>/login` with the token; an API client sends it as a bearer.

The flow end to end:

1. `ngrok http http://127.0.0.1:8000` in one terminal. A free ngrok account has one static domain;
   use it (`ngrok http --url=https://<your-domain> ...`) so the URL survives restarts.
2. In another: `export COVERSET_PUBLIC_URL=https://<your-domain> COVERSET_ADMIN_TOKEN=$(openssl rand
   -hex 32)`, then `uv run coverset dev --tunnel` (or `make dev-hosted` with the same variables, for the
   Postgres + MinIO shape behind the tunnel).
3. Open the URL, sign in with the token. For webhooks from real repositories, create a GitHub App at
   `<url>/setup` ([github.md](github.md)): it is created on the tunnel's URL, so keep the domain.
4. Ctrl-C stops the server; stop ngrok separately.

ngrok's free tier shows browsers an interstitial page before the first request (webhooks and API
clients are not affected). `coverset deploy check` with these settings warns that the bind is
loopback ("only a proxy or tunnel on this host can reach it"), which is exactly the setup.

## Signing in locally

| Mode | When | Who you are |
|---|---|---|
| open mode | no `COVERSET_ADMIN_TOKEN`, loopback bind (the default) | every browser is the local operator, an instance admin; `/login` is a 404 because there is nothing to sign in with |
| admin token | `COVERSET_ADMIN_TOKEN` set (32+ characters) | signed out until you sign in with the token on `/login` |
| development sign-in | `COVERSET_DEV_LOGIN=true` plus the conditions below | whoever you say, with the role and repository permissions you pick |
| GitHub | a GitHub App's client id and secret (from `/setup`) | your GitHub account; needs a public URL GitHub can redirect to: see [deploy.md](deploy.md) and [auth.md](auth.md) |

The development sign-in lets you click through the RBAC rules as a member without a GitHub App.
It appears on `/login` as **Sign in as a local user**: a login, a role (member or admin), and for
members a permission per repository in the database (read, write, admin or none). Submitting it
creates or updates a `users` row with `forge = "fake"` and `external_id = "dev:<login>"`, replaces
that member's `repo_access` rows with the picker's choices, and opens a server-side session. It
uses the same session code as the GitHub callback, and audits `login` with
`{"via": "dev", "dev": true}`. In open mode that session takes the operator's place. Sign out to be
the operator again.

It is offered only when all of these hold; otherwise the form is absent and `POST /login/dev` is a
404 (`tests/web/test_dev_login.py` breaks each one):

- `COVERSET_DEV_LOGIN=true` (default false);
- `COVERSET_ENV=dev` (the default);
- open mode: no `COVERSET_ADMIN_TOKEN`. In token mode the form would make any local caller an
  instance admin without the token, so it is not offered there;
- a loopback `COVERSET_BIND`, and a `COVERSET_PUBLIC_URL` nothing outside the machine can reach
  (empty, `localhost`, `*.localhost`, `*.test`);
- the fake forge with no GitHub credentials: no `COVERSET_GITHUB_APP_ID`, `COVERSET_GITHUB_TOKEN` or
  App client secret;
- the request's `Host` is a loopback name.

With `COVERSET_DEV_LOGIN=true`, `coverset dev` says at startup whether the sign-in is on
(`development sign-in on at http://127.0.0.1:8123/login`) or which condition turned it off
(`development sign-in off: COVERSET_ENV is prod, not dev`).

Testing RBAC as a member, as run for this page against the demo:

```sh
COVERSET_DEV_LOGIN=true uv run coverset dev --bind 127.0.0.1:8123
# browser: http://127.0.0.1:8123/login → Sign in as a local user
#   login dana, role member, coverset-demo/ledger: read → Sign in
```

Result: `/setup`, `/audit` and `/forge/outbox` answered 403 ("Instance admins only"), while `/`,
`/repos/1` and `/account` answered 200 with `@dana` in the header. After **Sign out**, `/setup` was
200 again (the operator).

A session from this form counts only while every condition still holds, checked on each request.
Turn the flag off, set `COVERSET_ENV` to anything but dev, set an admin token or bind beyond
loopback, and the browser is the operator again (open mode) or signed out (token mode). The access
guard treats a personal API token of such a user the same way; the upload endpoint
(`POST /v1/bundles`) checks tokens itself and does not yet. `tests/web/test_dev_login.py` signs in
as an admin through the form and then breaks each condition in turn.

## Exercising runs without GitHub

The fake forge records every call it would have made to GitHub: check runs, the sticky PR comment.
It is the default whenever no App or token is configured.

| What | How | Seen |
|---|---|---|
| a recorded webhook | `coverset replay <fixture> --server http://127.0.0.1:8123` posts `fixtures/webhooks/<fixture>.json`. The server refuses the fixtures' test secret outside `COVERSET_ENV=test`, so give the server and `replay` the same `COVERSET_GITHUB_WEBHOOK_SECRET` (16+ characters); `replay` re-signs with it | `ping` → `HTTP 202 {"status":"ignored_event"}`; `pr_synchronize --new-delivery` → `202 duplicate` (the demo already ran that head) |
| runs from webhooks | the local executor refuses webhook-triggered runs unless `COVERSET_LOCAL_ALLOW_WEBHOOK_RUNS=true` (the demo sets it for its own process) | |
| a run on a working tree | `coverset run --repo PATH --head main` (a path is registered on first use; `--base` for a PR-style change) | the demo repository's 97 tests, `completed: success`, 4.2 s |
| what the forge was asked | `/forge/outbox` in the UI, or `coverset forge calls --limit 5` | `create_check`, `update_check`, `upsert_pr_comment` rows |
| upload mode | a repository token, `pytest --coverset` in the project, then `coverset-ci upload` against the dev server | verdict `success (97 passed)` in 0.9 s |

The CLI prints run links with `COVERSET_BIND` (`http://127.0.0.1:8000/...` by default), not the
dev server's `--bind`, so the links point at the wrong port unless the two agree.

Upload mode, end to end, on a clone of the demo repository:

```sh
uv run coverset users tokens create --name dev-upload --repo coverset-demo/ledger --scopes upload
git clone "$COVERSET_DATA_DIR/demo.git" /tmp/ledger && cd /tmp/ledger
/path/to/coverset/.venv/bin/python -m pytest --coverset      # writes .coverset/ (97 tests)
COVERSET_TOKEN=cst_... /path/to/coverset/.venv/bin/coverset-ci upload \
    --server http://127.0.0.1:8123 --repo coverset-demo/ledger \
    --sha "$(git rev-parse HEAD)" --branch main --with-sources --wait
```

## Iterating on a page

- **Templates** (`src/coverset/web/templates/`) reload without a restart. `layout.html` is the
  page shell. A page asked for by htmx (`HX-Request: true`) gets the same template without the
  shell (`_fragment.html`). For `/repos/1`, `curl -H 'HX-Request: true'` returned a 62 KB
  fragment against a 68 KB full page.
- **Render modules** (`src/coverset/web/render/*.py`: treemap, source, ribbon, timeline,
  sparkline, shape grid) build HTML in Python and escape through `render/html.py`. Editing one
  restarts the server.
- **Static files** (`app.js`, `app.css`, `features.css`) are served fresh as described above.
  `curl -I` showed `Cache-Control: public, max-age=31536000, immutable` for the current `?v=` URL
  and `no-cache` without one.
- **Keys**: the keyboard shortcuts live in the `KEYS` table of `web/static/app.js`, the help
  dialog in `layout.html`, and the Keys table of [ui.md](ui.md); the static report has its own in
  `report/static/report.js`. `tests/web/test_keys_table.py` fails when they disagree.
- **The static report for the same data**: `coverset report --run <run_id> --out DIR` wrote 159
  pages for the uploaded run in 0.9 s; `coverset report .coverset` inside the project does the same
  from the plugin's output. Its templates are under `src/coverset/report/` and are read once per
  build.

## Tests

| Layout | |
|---|---|
| `tests/` | the control plane, by package: `web/`, `orchestrator/`, `facts/`, `forge/`, `report/`, `analysis/`, `executors/`, `lang/`, `agent/`, `ai/`, `model/`, `plugin_conformance/`, `bench/`, and `e2e/` |
| `tests/web/conftest.py` | runs the real local demo once per session (about 20 s) for the page tests |
| `packages/pytest-coverset/tests/` | the plugin, driven through `pytester` (a pytest inside the test) |
| `packages/coverset-vitest/test/` | the Vitest reporter (`npm test`) |

Markers (`pyproject.toml`): `security`, `slow` (end to end), `modal` (live Modal, skipped without
a token), `network` (needs the package index). On 2026-10-14 the suite collected 2,703 tests;
`-m "not slow and not modal and not network"` kept 2,635 and `-m security` 829. The quick selection
is barely faster than the whole suite with six workers, because the page tests' shared demo
fixture dominates both; while you work, run the module you are changing.

| Command | What | Time |
|---|---|---|
| `make check` | `ruff check`, `ruff format --check`, then the quick tests with `-n 6` | tests: 126 s (measured when the selection held 2,623) |
| `make test` | the whole suite, `pytest -q -n 6 -p no:cacheprovider` | 2,703 in 131 s |
| `make test-plugin-312` | the plugin's tests on Python 3.12 in an isolated environment | 237 passed, 23.7 s |
| `make test-vitest` | the reporter's Vitest suite | 156 passed, 0.9 s |
| `make test-pg` | the whole suite against the dev stack's Postgres ([Hosted-like development](#the-test-suite-against-both)) | |
| `uv run pytest tests/test_dev_command.py` | one module while you work on it | 0.4 s |

`make test-plugin-312` installs the two local packages with `--with-editable`. uv caches the wheel
it builds from a local directory until that directory's `pyproject.toml` changes, so `--with
./packages/pytest-coverset` tested an old copy of the plugin: 5 collection errors for modules
added since that wheel was built.

The end-to-end tests skip what they cannot run: `test_vitest_reporter.py` needs `node`, `npm`,
`examples/ts-app/node_modules` (`npm install` there) and the reporter built into it (`npm run
refresh-reporter`); `test_rust_adapter.py` needs `cargo`, `rustc` and `llvm-tools-preview`
(`cargo-nextest` for the nextest case); the demo tests need `uv` and `git`.

## Debugging

| Tool | Use |
|---|---|
| the server's log | requests, warnings, background tasks; `COVERSET_LOG_LEVEL=DEBUG` adds Alembic's lines |
| `COVERSET_FAKE_NOW=2026-10-10T12:00:00Z` | pins "now" (`Settings().now()` printed `2026-10-10 12:00:00+00:00`); the screenshots use it |
| `coverset facts tables --repo coverset-demo/ledger --run <id>` | every facts table of a run: kind, files, rows, bytes |
| `coverset facts explain --repo coverset-demo/ledger --run <id>` | the run row, its timeline, shards and ingest log |
| `coverset doctor --repo PATH [--no-collect]` | what coverset would do with a repository, without running its tests (0.6 s on the demo clone with `--no-collect`) |
| `coverset deploy check --offline` | the configuration as a deployment sees it: on the dev data directory, 5 PASS and 7 WARN (open mode, local public URL, no App, no webhook secret) |
| `coverset forge calls` | the fake forge's outbox from the shell |
| `coverset mcp serve --source .coverset` | the agent tools over stdio against a local `pytest --coverset` output; an `initialize` plus `tools/list` probe listed 15 tools (`list_repos` … `explain_shape_key`) in 0.8 s; [mcp.md](mcp.md) has the client configuration |

## Conventions

- **Style**: `ruff check` and `ruff format` (line length 110, Python 3.13); `make format` applies
  both. Docstrings say what a function does and why; type hints everywhere.
- **Migrations**: change `db/models.py`, then `uv run alembic revision --autogenerate -m "..."`
  (the `alembic.ini` post-write hooks run ruff on the new file), read the generated revision, and
  run `uv run coverset init-db`. `uv run alembic check` against a migrated database answered
  "No new upgrade operations detected." `tests/test_models.py::test_every_foreign_key_column_leads_an_index`
  fails for a foreign key column that does not lead an index.
- **Byte-stable reports**: the report tests set `SOURCE_DATE_EPOCH` (`tests/report/conftest.py`), and
  the footer date comes from it, so two builds of the same data are byte-identical.
- **No inline JavaScript**: the Content-Security-Policy is `script-src 'self'`. Behaviour goes in
  `app.js`, keyed off `data-*` attributes; no `on*=` handlers and no `hx-on`.
  `web_helpers.assert_csp_clean` checks rendered pages. Jinja autoescape stays on.
- **CSS**: colours are tokens in `app.css` with `light-dark()`, so both themes come from one rule.
