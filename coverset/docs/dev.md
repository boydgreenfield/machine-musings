# Developing coverset

How to run coverset on your own machine and change it quickly: one command for a seeded server
that reloads, a quick data reset, a sign-in that needs no GitHub App, and the tests to run before
you push. Everything below was run on a MacBook (Apple silicon) on 2026-10-14. The timings are from
that machine.

- [Setup](#setup)
- [The 30-second loop](#the-30-second-loop)
- [Data](#data)
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
| docker | optional: `deploy/compose.yml`, the Dockerfile | |
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
0600). After the demo and one upload:

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
