# Postgres

coverset's control-plane database (repositories, PRs, runs, tests, results, events, deliveries,
accounts, audit log; [data-model.md](data-model.md)) is Postgres, 14 or newer: on a laptop the local
server (Homebrew's is the easiest), in a deployment a managed one (Neon, Supabase, RDS) or the one
in `deploy/compose.yml`. Together with an object store (`COVERSET_BLOB_URL`,
[object-store.md](object-store.md)) the containers keep no state, and several replicas can serve
one URL.

- [The database coverset uses](#the-database-coverset-uses)
- [Development: Homebrew or the compose stack](#development-homebrew-or-the-compose-stack)
- [Connection URLs](#connection-urls)
- [What every connection gets](#what-every-connection-gets)
- [Pooling](#pooling)
- [Schema and migrations](#schema-and-migrations)
- [Several replicas: the leader lease](#several-replicas-the-leader-lease)
- [Backups](#backups)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)

Code: `src/coverset/db/` (`session.py` engines, creating the database and migrations, `upsert.py`,
`leader.py`, `testing.py`, `migrations/`). Tests: `tests/db/`, `tests/test_models.py`.

## The database coverset uses

`COVERSET_DATABASE_URL`, by default `postgresql+asyncpg://localhost/coverset`: the server on this
machine, database `coverset`, as the current user without a password (what Homebrew's Postgres
accepts). Any other server is `postgresql://user:password@host:5432/name`; anything that is not a
Postgres URL for asyncpg is refused at start-up with the fix in the message.

`coverset init-db`, `coverset dev`, `coverset demo`, every CLI command that touches the database and
`coverset serve` (with `COVERSET_MIGRATE_ON_START`, the default) **create the database** when the
server has none of that name: they connect to the server's maintenance database `postgres` as the
URL's user, run `CREATE DATABASE "<name>"` (only for a plain identifier: letters, digits and
underscores, at most 63 characters; anything else is refused with "create it yourself"), then
migrate it. The user needs the `CREATEDB` privilege for that (Homebrew's user has it; on a managed
server create the database once in its console). A server that does not answer stops the command
with exit status 2 and one line:

```
Error: cannot reach Postgres at localhost:5432 (Connect call failed ...): start it (brew services start postgresql@14), or run `make services` and set COVERSET_DATABASE_URL=postgresql://coverset:coverset@127.0.0.1:55432/coverset
```

One database belongs to one data directory: the rows name the facts, uploads and mirrors under it
(or under its bucket prefix). Point a second data directory at a database of its own
(`COVERSET_DATABASE_URL=postgresql+asyncpg://localhost/coverset_other`, created on first use).
`coverset demo --fresh` starts the demo over: it drops the database (`DROP DATABASE ... WITH
(FORCE)`, only on a server on this machine, never the maintenance database), empties the bucket
prefix when `COVERSET_BLOB_URL` is set, empties the cache directory and the data directory, then
seeds again into a database it creates.

## Development: Homebrew or the compose stack

The local server is a development requirement:

```sh
brew install postgresql@14 && brew services start postgresql@14
uv run coverset init-db   # ... database ready (11 migration(s) applied): postgresql+asyncpg://localhost/coverset (Postgres 14.17)
```

Any Postgres 14 or newer on `localhost:5432` that lets the current user in works the same way.
`deploy/dev-compose.yml` runs Postgres 17 and MinIO (an S3-compatible store) with throwaway
credentials, on 127.0.0.1 only, for the hosted shape and for testing against 17:

| Service | Address | Credentials |
|---|---|---|
| `postgres` (`postgres:17`) | `127.0.0.1:55432`, database `coverset` | `coverset` / `coverset` |
| `minio` (`cgr.dev/chainguard/minio`) | API `127.0.0.1:9000`, console `http://127.0.0.1:9001` | `minioadmin` / `minioadmin` |
| `minio-init` | creates the bucket `coverset`, then idles (healthy once the bucket exists) | |

Postgres is published on **55432**, not 5432, so it never collides with the local server.
`COVERSET_DEV_PG_PORT`, `COVERSET_DEV_S3_PORT` and `COVERSET_DEV_S3_CONSOLE_PORT` move the ports.
MinIO's own images (`minio/minio`, `minio/mc`) no longer pull from Docker Hub or quay.io;
Chainguard's build of MinIO is used instead, for the server and for the bucket set-up.

```sh
make services            # docker compose -f deploy/dev-compose.yml up -d --wait (seconds once the images are pulled)
make services-down       # stop, keep the data
make services-reset      # stop and delete both volumes
make services-psql       # psql in the container
make test-pg             # the whole suite against this Postgres 17 (leaves it running)

export COVERSET_DATABASE_URL=postgresql://coverset:coverset@127.0.0.1:55432/coverset
uv run coverset init-db  # ... database ready (11 migration(s) applied): postgresql+asyncpg://coverset:***@127.0.0.1:55432/coverset (Postgres 17.11)
```

`make dev-hosted` (`coverset dev --hosted`, [dev.md](dev.md)) starts the stack when it is down and
serves against its Postgres and MinIO. `docker compose ... up --wait` on Compose before 2.30 fails
when a container of the project has exited, even with status 0, which is why `minio-init` stays up.

## Connection URLs

Paste the provider's URL. `postgresql://` and `postgres://` become `postgresql+asyncpg://`
(`config.py`); libpq parameters asyncpg does not know are translated by the engine
(`session.postgres_connect_args`): `sslmode` becomes asyncpg's `ssl` (same values), `connect_timeout`
its `timeout`, and `channel_binding` is dropped. Other drivers (`postgresql+psycopg`) are refused.

| Provider | `COVERSET_DATABASE_URL` | Notes |
|---|---|---|
| the local server (Homebrew) | `postgresql+asyncpg://localhost/coverset` (the default) | development |
| this repo's compose stack | `postgresql://coverset:coverset@127.0.0.1:55432/coverset` | development, Postgres 17 |
| Neon | `postgresql://<user>:<pw>@ep-<name>.<region>.aws.neon.tech/neondb?sslmode=require` | the **direct** host, not `ep-<name>-pooler...` (see [Pooling](#pooling)); a compute that scales to zero adds a cold start to the first query after idling |
| Supabase | `postgresql://postgres:<pw>@db.<ref>.supabase.co:5432/postgres?sslmode=require`, or the session pooler `postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres` | the direct host may be IPv6-only on your plan; the session pooler (port 5432) is fine, the transaction pooler (port 6543) is not |
| AWS RDS / Aurora | `postgresql://<user>:<pw>@<instance>.<id>.<region>.rds.amazonaws.com:5432/coverset?sslmode=require` | `verify-full` needs the RDS CA bundle in the system trust store; use the instance (or cluster writer) endpoint, not RDS Proxy: coverset pools its own connections and needs session semantics ([Pooling](#pooling)) |
| Fly Postgres | `postgres://<user>:<pw>@<app>.flycast:5432/<db>?sslmode=disable` | `fly postgres attach` prints it as `DATABASE_URL`; coverset reads only `COVERSET_DATABASE_URL`. Fly's private network is already encrypted |

`coverset deploy check` connects and reports, in its `database` row, the server version, the
revision against the packaged head, the pool a process opens and who holds the background-task
lease (a database the server does not have yet is a failing row that names `coverset init-db`):

```
$ uv run coverset deploy check --offline
...
PASS  database        Postgres 14.17, migrated to head 0011; 10 + 10 overflow connections per process; lease coverset:background: free (no process runs the background tasks right now)
```

`db.session.check_database(url)` is the same report as a value (it unpacks as `(status, detail)`
and carries `server_version`, `current`, `heads`, `pool`, `leader`, `error`), and
`db.leader.leader_status(engine)` the lease holder alone (of this database: advisory locks are per
database, and `pg_locks` lists every database's, so another installation's lease on the same
server, a developer's `coverset dev` say, is not reported as this one's).

## What every connection gets

`db.session.make_engine` (all of coverset goes through it):

| Setting | Value | Why |
|---|---|---|
| pool | 10 connections, up to 10 more under load | at most 20 per process; the web app's dispatcher, runs, SSE streams and requests share it |
| `pool_pre_ping` | on | a server restart or an idle-timeout on a proxy is noticed before a request uses the dead connection |
| `pool_recycle` | 1800 s | connections are replaced every 30 minutes |
| `application_name` | `coverset` (a URL's `application_name` wins) | `pg_stat_activity` shows which sessions are coverset's |
| `timezone` | `UTC` | anything the server renders as text matches the app (values are `timestamptz` either way) |
| `tcp_keepalives_idle` / `_interval` / `_count` | 60 s / 10 s / 3 | the server probes an idle client after 60 s and gives up after 3 probes 10 s apart, so a replica whose host vanished (VM crash, power loss, a partition) loses its sessions, with the leader lease and any row locks, after about 90 s; the OS default on Linux is 2 h 11 min. Ignored where the transport has no keepalives (a Unix socket) |
| `statement_timeout` | **30 s in the web app**, none elsewhere | no request or background task of `serve` waits on one statement longer than 30 s; CLI commands, migrations (the migration transaction sets it to 0) and the retention gc's bulk deletes (`SET LOCAL statement_timeout = 0` for their transaction, so a backlog larger than 30 s can delete still shrinks) run without one |

## Pooling

coverset needs **session-mode** connections. Transaction-mode pooling (PgBouncer
`pool_mode=transaction`, Neon's `-pooler` endpoint, Supabase's port 6543, RDS Proxy's multiplexing)
is not supported, for two reasons:

1. The [leader lease](#several-replicas-the-leader-lease) is a session-level advisory lock. A
   transaction pooler hands a session's statements to whichever server connection is free, so the
   lock would be taken on one server session and "held" by a client that no longer has it.
2. asyncpg prepares every statement on its connection. Behind a transaction pooler the next
   statement may land on a server connection that never saw the `PREPARE` (or already has one of the
   same name). PgBouncer 1.21+ with `max_prepared_statements` handles that, but not (1).

A **session-mode** PgBouncer works, with one more line in its configuration: coverset sends
`statement_timeout` (the web app) and the three `tcp_keepalives_*` settings as startup parameters,
which PgBouncer refuses unless they are listed:
`ignore_startup_parameters = statement_timeout,tcp_keepalives_idle,tcp_keepalives_interval,tcp_keepalives_count`.
Ignored means not applied to the server connection: behind PgBouncer there is no 30 s web timeout,
and a vanished replica is noticed by PgBouncer's own `tcp_keepalive` settings instead.

So: point coverset at the direct (or session-pooler) URL. Its own pool bounds the connections: at
most 20 per replica, plus one or two for each CLI command while it runs: three replicas need about 60
connections, so check the plan's connection limit. `coverset deploy check`
warns when the host looks like a transaction pooler (`-pooler.` in the name, or Supabase's port
6543).

## Schema and migrations

One Alembic chain (`0001`-`0011`) builds the schema. Every constraint has a name (the metadata's
naming convention), and `tests/db/test_postgres_migrations.py` runs the whole chain on an empty
database, compares the result with the models (what `alembic check` does), downgrades to base
(only `alembic_version` is left, and no sequences) and upgrades again. Nothing in the schema or in
coverset's SQL needs more than Postgres 14 (no `MERGE`, no `CREATE DATABASE ... STRATEGY`, no
`NULLS NOT DISTINCT`); the chain and `alembic check` were run on 14.17 and 17.11.

| Type | Postgres |
|---|---|
| integer ids (`BigIntId`) and the foreign keys to them | `BIGINT`, sequences `AS bigint` (revision `0009`) |
| datetimes (`UTCDateTime`) | `timestamptz`; aware UTC in and out of asyncpg |
| JSON columns | `json`, not `jsonb` |
| string enums (`str_enum`) | `VARCHAR(32)`, no CHECK |
| digests (`LargeBinary`) | `bytea` |

**Why BIGINT ids:** a `SERIAL` is 32-bit and ids are never reused, while the web accepts any id up
to 2**63 - 1 in a URL (a larger one must answer 404, not fail on an int4 overflow). Revision `0009`
widens the columns in one `ALTER TABLE` per table. `0010` adds `ix_run_events_at`, the index the retention gc's `DELETE FROM run_events WHERE at <
cutoff` uses (the largest table; without it the daily delete scanned all of it). `0011` makes
`settings.value` `text` (it was `VARCHAR(2048)`): the sealed GitHub App `/setup` stores, private key
included, is about 2.7 KB ([security.md](security.md#5-secrets)); on Postgres the change rewrites no
rows.

**Why `json`, not `jsonb`:** coverset never filters, indexes or compares JSON in SQL (it reads whole
documents), so `jsonb`'s operators and indexes would buy nothing yet. The one rule that follows:
never put a JSON column in `DISTINCT`, `GROUP BY`, `UNION` or an equality, because Postgres has no
equality operator for `json`. Switching later is one revision (`ALTER COLUMN ... TYPE jsonb USING
col::jsonb`).

Postgres-specific SQL lives in the database layer: upserts go through `db.upsert.upsert`
(`INSERT ... ON CONFLICT`; `orchestrator/history.py`'s `test_stats` and `test_attempts` rows and
the Modal executor's `envs` row use it), the lease and transaction locks through `db.leader`,
migrations through `db.session`. An upsert writes its rows in key order: two runs
recording `test_stats` for the same tests at once then lock the rows in the same order, and the
second waits instead of failing with `deadlock detected` (which 4,000 rows in opposite orders did
every time before; `tests/db/test_upsert.py`). Should a deadlock (SQLSTATE `40P01`) or a
serialization failure (`40001`) still happen, the orchestrator's short transactions
(`orchestrator/planner.run_transaction`) run again with backoff: asyncpg's errors reach SQLAlchemy
as a plain `DBAPIError`, so the retry looks at the SQLSTATE. A statement timeout (`57014`) is not retried (`tests/db/test_transaction_retry.py`, which
also provokes a real deadlock).

**Running them.** `coverset init-db` creates the database when it is missing, migrates and prints
each revision and the server version. `coverset serve` does the same before it opens the port
(`COVERSET_MIGRATE_ON_START`, [deploy.md](deploy.md#migrations-on-deploy)). A migration takes the
transaction-scoped advisory lock `coverset:migrate`, so replicas that start together migrate one
after the other, and the second finds the schema at head.
Checking whether a database is at head costs one read of `alembic_version`; the revision scripts are
loaded only when there is something to apply. For development, `uv run alembic upgrade head`,
`uv run alembic check` and `uv run alembic revision --autogenerate -m "..."` honour
`COVERSET_DATABASE_URL` too (the database must exist: `coverset init-db` first).

## Several replicas: the leader lease

Any number of `coverset serve` processes can share one Postgres database: each serves the UI,
webhooks, the API, uploads and SSE. Four loops must run in exactly one of them: the **run dispatcher**
(and so every run's execution), the **retention gc**, the **sandbox reaper** and the **warm-pool
maintainer**. That one is the holder of the lease `coverset:background`
(`db/leader.py`), a session-level `pg_try_advisory_lock` on a 64-bit hash of the name, held on one
of the process's pooled connections for as long as the process lives:

- At start each replica tries once and logs its role:
  `leader: <host>:<pid> runs the dispatcher, gc, reaper and pool maintainer (lease coverset:background)`
  or `follower: <host>:<pid> serves requests; another replica holds lease coverset:background (re-checked every 30 s)`.
- Every 30 s the holder checks that its session still has the lock and each follower tries to take
  it. Nothing expires: the lock lives as long as the holder's session, so a clean stop (or a kill:
  the OS closes the socket) hands it over at the next follower check (measured: 29 s after killing
  the leader of two local replicas). A host that vanishes without closing anything (VM crash, power
  loss, a network partition) is noticed through the TCP keepalives every connection asks for
  (60 s idle, then 3 probes 10 s apart: about 90 s), so a takeover completes within about two
  minutes; without them Postgres would keep the dead session, and the lease, for the OS default of
  about 2 h on Linux.
- `run_while_leader` starts each loop when the lease is gained and cancels it when it is lost.
  Losing the lease stops dispatching; runs already executing keep their claims and heartbeats. A
  loop that crashes (the dispatcher's boot handling failing on the very Postgres blip that moved the
  lease, say) is logged and started again 5 s later while the lease is held, so a holder never sits
  on the lease running nothing.
- The lease decides who works, never correctness: a run is claimed with an atomic
  `UPDATE runs ... WHERE status = 'queued'`, which under READ COMMITTED lets exactly one of several
  concurrent claimants win (`tests/db/test_concurrency.py`).

What changes with several replicas:

| Thing | Behaviour |
|---|---|
| a run queued through a follower (webhook, API, upload) | the leader's dispatcher polls every 2 s, so it starts within about 2 s (in-process wake-ups only reach the replica that queued it) |
| boot handling | the lease holder abandons queued runs created before it started (unless `COVERSET_REQUEUE_ON_BOOT=true`) and keeps later ones, which a live replica queued; set `COVERSET_REQUEUE_ON_BOOT=true` with several replicas so a takeover never drops queued work. Among `running` runs claimed by a `serve` on the same host it abandons only those whose pid is gone: a live pid is another replica on that host, or the previous leader finishing its runs |
| a replica that dies mid-run | the lease holder abandons (or re-queues) `running` runs whose heartbeat is older than 5 minutes, checking every 60 s; if the dead replica was the leader, its successor's first sweep runs once it has the lease (within about 2 minutes of a vanished host, see above) |
| two uploads of one commit at two replicas | `register_upload` takes a transaction-scoped advisory lock per commit, so they join one run with shards 0 and 1 |
| runs' capacity | all runs execute in the leader: `COVERSET_MAX_CONCURRENT_RUNS` is the instance's limit, not per replica |
| rate limits (per process) | login 20 attempts per minute per address, public verdict `COVERSET_PUBLIC_VERDICT_RPM` (and twice that for polling), bearer API 1,200/min, uploads 600/min: with N replicas a client may get up to N times as many unless the load balancer pins it |
| caches (per process) | scoped settings for 30 s (a change made on another replica shows within 30 s), facts and page caches keyed by what they read ([object-store.md](object-store.md) for the blob side) |
| SSE streams | poll `run_events` every 0.5 s, so any replica can serve any stream; no sticky sessions |
| sign-in | sessions are rows (`user_sessions`); set the same `COVERSET_SECRET_KEY` on every replica so they accept one another's cookies |
| the GitHub App `/setup` stores | a row of `settings`, sealed under `COVERSET_SECRET_KEY` (the same on every replica): the replica that stored it uses it at once, every other one within 5 s (one indexed read of the row's `updated_at` per check), without a restart |
| the Modal executor's instance id | a row of `settings` too, so every replica tags its sandboxes alike and any replica's reaper finds them |

## Backups

The provider's: Neon, Supabase and RDS keep point-in-time recovery (the retention depends on the
plan); Fly Postgres takes daily volume snapshots; `deploy/aws/user-data.sh` writes a nightly
`pg_dump --format=custom` of the compose stack's database to the data disk. `pg_dump` is also the
way to move an instance to another server (`pg_restore` into an empty database there, then point
`COVERSET_DATABASE_URL` at it); the blobs move with the bucket or the data directory
([object-store.md](object-store.md)).

## Tests

The suite runs on a Postgres server and nothing else: the local one by default
(`postgresql+asyncpg://localhost/postgres`, the maintenance database of Homebrew's server), or the one
`COVERSET_TEST_DATABASE_URL` names (an asyncpg URL whose user may `CREATE DATABASE`). When it does
not answer, `pytest` stops before collecting anything, with one line saying how to start it; no
test is skipped for want of a database.

Each pytest-xdist worker migrates one template database once, and every test that takes the
`settings` fixture gets `CREATE DATABASE <fresh> TEMPLATE <template>`, already at head, dropped
(`WITH (FORCE)`) before the next test's (`coverset.db.testing.PostgresTestDatabases`). The page and
report tests' shared demos (`tests/web/conftest.py`, `tests/report/conftest.py`) and the other
module-scoped demos are seeded into a database of their own from the same template; tests read or
doctor such a database through `coverset.db.testing.run_sql`. Each worker also points
`COVERSET_DATABASE_URL` at a scratch database of its own on the same server: code that reads the
process environment (CLI commands under `CliRunner`, subprocesses) and `Settings.for_tests()` built
without a `database_url` (the one environment key the hermetic settings read) both land there, never
in a developer's `coverset` database. Everything the suite creates is dropped at the end of the
session.

```sh
make test      # the local server
make test-pg   # = make services, then COVERSET_TEST_DATABASE_URL=postgresql+asyncpg://coverset:coverset@127.0.0.1:55432/postgres uv run pytest -q -n 6
```

Measured on 2026-10-14 (six workers, an M4 Max MacBook):

| Server and blobs | Passed | Skipped | Failed | Time |
|---|---|---|---|---|
| Homebrew Postgres 14.17, local files (`make test`) | 3,071 | 7 | 0 | 143 s |
| compose Postgres 17.11, local files (`COVERSET_TEST_DATABASE_URL=...@127.0.0.1:55432/coverset`) | 3,071 | 7 | 0 | 145 s |
| Homebrew Postgres 14.17, MinIO (`COVERSET_TEST_BLOB_URL`; measured before the vault, with 3,048 tests) | 3,048 | 7 | 0 | 176 s |

The skipped tests are opt-in (a live Modal account, a Docker image build, the package index). A
clone of the template, with the drop of the previous test's, costs about 70 ms on Homebrew's 14 (it
copies the files and checkpoints) and 25 ms on the compose stack's 17; a `TRUNCATE` of every table
would cost a third of that, but a fresh database also ends connections a test leaked and resets
every sequence, which is worth the 70 ms.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `TypeError: connect() got an unexpected keyword argument` | a driver parameter asyncpg does not take; `sslmode`, `connect_timeout` and `channel_binding` are handled, others (`options`, `gssencmode`) must go |
| `make services` fails with "Ports are not available ... 5432" | an older copy of the compose file; this one uses 55432 (or set `COVERSET_DEV_PG_PORT`) |
| a request fails with `canceling statement due to statement timeout` | a statement of the web process ran 30 s; find it in the server log, and run large one-off work from the CLI (no timeout) |
| `remaining connection slots are reserved` / `too many connections` | replicas × 20 plus CLI commands exceed the server's limit: fewer replicas, a bigger plan, or a session-mode pooler in front |
| `cannot reach Postgres at localhost:5432` | the local server is not running: `brew services start postgresql@14` (or `make services` and the compose URL) |
| `cannot create database coverset ... permission denied` | the URL's user may not `CREATE DATABASE`: create it once (`createdb coverset`, or the provider's console) |
| no replica dispatches runs | `coverset deploy check --offline` shows the lease holder in its `database` row; a holder that is wedged but connected keeps it: restart it. A holder whose dispatcher loop crashed logs `run dispatcher failed; it starts again in 5 s` and retries by itself |
| the lease stays with a replica whose host is gone | expected for up to about 2 minutes (TCP keepalives); longer means the server ignores the keepalive settings (a pooler in front, or a provider that fixes them): set the provider's `tcp_keepalives_*` or `idle_session_timeout`, or restart the dead host's successor |
| `prepared statement "__asyncpg_stmt_..." does not exist` | a transaction-mode pooler: use the direct URL ([Pooling](#pooling)) |
