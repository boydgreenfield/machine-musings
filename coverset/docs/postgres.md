# Postgres

coverset's control-plane database (repositories, PRs, runs, tests, results, events, deliveries,
accounts, audit log; [data-model.md](data-model.md)) runs on SQLite or Postgres from one set of
models and one set of migrations. SQLite is the default and needs nothing: a laptop, the demo and a
single VM keep using a file under the data directory. Postgres is for a hosted control plane: a
managed database with its provider's backups, containers that keep no state
(`COVERSET_DATABASE_URL` plus `COVERSET_BLOB_URL`, [object-store.md](object-store.md)), and several
replicas behind one URL.

- [Choosing](#choosing)
- [Development: the compose stack](#development-the-compose-stack)
- [Connection URLs](#connection-urls)
- [What every connection gets](#what-every-connection-gets)
- [Pooling](#pooling)
- [Schema and migrations](#schema-and-migrations)
- [Several replicas: the leader lease](#several-replicas-the-leader-lease)
- [Backups](#backups)
- [Moving a SQLite instance to Postgres](#moving-a-sqlite-instance-to-postgres)
- [Tests on Postgres](#tests-on-postgres)
- [Troubleshooting](#troubleshooting)

Code: `src/coverset/db/` (`session.py` engines and migrations, `upsert.py`, `leader.py`,
`copy.py`, `testing.py`, `migrations/`), `src/coverset/commands/db_cmd.py` (`coverset db`).
Tests: `tests/db/`, `tests/test_models.py`; `make test-pg` runs the whole suite on Postgres.

## Choosing

| | SQLite (default) | Postgres |
|---|---|---|
| `COVERSET_DATABASE_URL` | empty: `sqlite+aiosqlite:///<data_dir>/coverset.db` | `postgresql://user:pass@host:5432/db` (any libpq URL; coverset uses asyncpg) |
| Services to run | none | a Postgres server (tested on 17; the test suite's database clones need 15+) |
| Processes per database | one `serve` (plus CLI commands) | any number of replicas; one runs the background loops ([lease](#several-replicas-the-leader-lease)) |
| Writers | one at a time (WAL, 5 s busy timeout) | row locks, READ COMMITTED |
| Durability | the file; on Modal a 60 s backup loop to a Volume | the provider's (point-in-time recovery on Neon, Supabase, RDS) |
| Survives a container restart | only with a disk or Volume | yes, nothing local is needed (with `COVERSET_BLOB_URL`: `Settings.hosted`) |

Pick Postgres once the control plane runs anywhere but a laptop or one VM with a disk. A demo for
others from a laptop needs neither: `coverset dev` behind a tunnel ([deploy.md](deploy.md)).

## Development: the compose stack

`deploy/dev-compose.yml` runs Postgres 17 and MinIO (an S3-compatible store) with throwaway
credentials, on 127.0.0.1 only:

| Service | Address | Credentials |
|---|---|---|
| `postgres` (`postgres:17`) | `127.0.0.1:55432`, database `coverset` | `coverset` / `coverset` |
| `minio` (`cgr.dev/chainguard/minio`) | API `127.0.0.1:9000`, console `http://127.0.0.1:9001` | `minioadmin` / `minioadmin` |
| `minio-init` | creates the bucket `coverset`, then idles (healthy once the bucket exists) | |

Postgres is published on **55432**, not 5432: a Postgres installed on the laptop (Homebrew,
Postgres.app) usually holds 5432 already, and then Docker cannot bind it (on the machine these docs
were written on, Homebrew's Postgres 14 does). `COVERSET_DEV_PG_PORT`, `COVERSET_DEV_S3_PORT` and
`COVERSET_DEV_S3_CONSOLE_PORT` move the ports. MinIO's own images (`minio/minio`, `minio/mc`) no
longer pull from Docker Hub or quay.io; Chainguard's build of MinIO is used instead, for the server
and for the bucket set-up.

```sh
make services            # docker compose -f deploy/dev-compose.yml up -d --wait (seconds once the images are pulled)
make services-down       # stop, keep the data
make services-reset      # stop and delete both volumes
make services-psql       # psql in the container
make test-pg             # the whole suite against this Postgres (leaves it running)

export COVERSET_DATABASE_URL=postgresql://coverset:coverset@127.0.0.1:55432/coverset
uv run coverset init-db  # ... database ready (9 migration(s) applied): postgresql+asyncpg://coverset:***@127.0.0.1:55432/coverset (Postgres 17.11)
uv run coverset db status
```

`make dev-hosted` (`coverset dev --hosted`, [dev.md](dev.md)) starts the stack when it is down and
serves against Postgres and MinIO. `docker compose ... up --wait` on Compose before 2.30 fails when a
container of the project has exited, even with status 0, which is why `minio-init` stays up.

## Connection URLs

Paste the provider's URL. `postgresql://` and `postgres://` become `postgresql+asyncpg://`
(`config.py`); libpq parameters asyncpg does not know are translated by the engine
(`session.postgres_connect_args`): `sslmode` becomes asyncpg's `ssl` (same values), `connect_timeout`
its `timeout`, and `channel_binding` is dropped. Other drivers (`postgresql+psycopg`) are refused.

| Provider | `COVERSET_DATABASE_URL` | Notes |
|---|---|---|
| this repo's compose stack | `postgresql://coverset:coverset@127.0.0.1:55432/coverset` | development only |
| Neon | `postgresql://<user>:<pw>@ep-<name>.<region>.aws.neon.tech/neondb?sslmode=require` | the **direct** host, not `ep-<name>-pooler...` (see [Pooling](#pooling)); a compute that scales to zero adds a cold start to the first query after idling |
| Supabase | `postgresql://postgres:<pw>@db.<ref>.supabase.co:5432/postgres?sslmode=require`, or the session pooler `postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres` | the direct host may be IPv6-only on your plan; the session pooler (port 5432) is fine, the transaction pooler (port 6543) is not |
| AWS RDS / Aurora | `postgresql://<user>:<pw>@<instance>.<id>.<region>.rds.amazonaws.com:5432/coverset?sslmode=require` | `verify-full` needs the RDS CA bundle in the system trust store; use the instance (or cluster writer) endpoint, not RDS Proxy: coverset pools its own connections and needs session semantics ([Pooling](#pooling)) |
| Fly Postgres | `postgres://<user>:<pw>@<app>.flycast:5432/<db>?sslmode=disable` | `fly postgres attach` prints it as `DATABASE_URL`; coverset reads only `COVERSET_DATABASE_URL`. Fly's private network is already encrypted |

`coverset db status` connects and prints the backend, server version, revision against the
packaged head, the pool and the lease holder (`coverset deploy check` prints the same report as
its `database` row, without the lease line):

```
$ COVERSET_DATABASE_URL=postgresql://coverset:coverset@127.0.0.1:55432/demo_copy uv run coverset db status
database  postgresql+asyncpg://coverset:***@127.0.0.1:55432/demo_copy
backend   Postgres 17.11
revision  0010 (head 0010)
pool      10 + 10 overflow connections per process
leader    coverset:background: free (no process runs the background tasks right now)
status    pass: Postgres 17.11, migrated to head 0010; 10 + 10 overflow connections per process
```

`db.session.check_database(url)` is the same report as a value (it unpacks as `(status, detail)`
and carries `backend`, `server_version`, `current`, `heads`, `pool`, `error`), and
`db.leader.leader_status(engine)` the lease holder.

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
| `statement_timeout` | **30 s in the web app**, none elsewhere | no request or background task of `serve` waits on one statement longer than 30 s; CLI commands, migrations (the migration transaction sets it to 0), the retention gc's bulk deletes (`SET LOCAL statement_timeout = 0` for their transaction, so a backlog larger than 30 s can delete still shrinks) and `coverset db copy` run without one |

SQLite is unchanged: WAL, `synchronous=NORMAL`, `busy_timeout=5000`, enforced foreign keys, a shared
connection for `:memory:`.

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

One Alembic chain (`0001`-`0010`) builds the same schema on both backends; batch mode
(`render_as_batch`) is what lets SQLite alter tables and passes straight through to `ALTER TABLE`
on Postgres. Every constraint has a name (the metadata's naming convention), nothing uses
`sqlite_autoincrement`, and `tests/db/test_postgres_migrations.py` runs the whole chain on a fresh
Postgres database, compares the result with the models (what `alembic check` does), downgrades to
base (only `alembic_version` is left, and no sequences) and upgrades again.

| Type | SQLite | Postgres |
|---|---|---|
| integer ids (`BigIntId`) and the foreign keys to them | `INTEGER` (64-bit, the rowid alias) | `BIGINT`, sequences `AS bigint` (revision `0009`) |
| datetimes (`UTCDateTime`) | text, UTC, microseconds | `timestamptz`; aware UTC in and out of asyncpg |
| JSON columns | `JSON` (text) | `json`, not `jsonb` |
| string enums (`str_enum`) | `VARCHAR(32)`, no CHECK | the same |
| digests (`LargeBinary`) | `BLOB` | `bytea` |

**Why BIGINT ids:** a Postgres `SERIAL` is 32-bit and ids are never reused, while the web accepts any
id up to 2**63 - 1 in a URL (a larger one must answer 404, not fail on an int4 overflow). SQLite keeps
`INTEGER`: it is 64-bit already, and only `INTEGER PRIMARY KEY` auto-increments there. Revision
`0009` widens the columns on Postgres in one `ALTER TABLE` per table and does nothing on SQLite.
`0010` adds `ix_run_events_at`, the index the retention gc's `DELETE FROM run_events WHERE at <
cutoff` uses (the largest table; without it the daily delete scanned all of it).

**Why `json`, not `jsonb`:** the same migration files serve both backends, and SQLAlchemy's `JSON`
type is what SQLite has. coverset never filters, indexes or compares JSON in SQL (it reads whole
documents), so `jsonb`'s operators and indexes would buy nothing yet. The one rule that follows:
never put a JSON column in `DISTINCT`, `GROUP BY`, `UNION` or an equality, because Postgres has no
equality operator for `json`. Switching later is one revision (`ALTER COLUMN ... TYPE jsonb USING
col::jsonb`) and leaves SQLite alone.

Dialect-specific SQL lives in the database layer only: upserts go through `db.upsert.upsert`
(`INSERT ... ON CONFLICT` for both; `orchestrator/history.py`'s `test_stats` and `test_attempts`
rows and the Modal executor's `envs` row use it), the lease and transaction locks through
`db.leader`, migrations through `db.session`. An upsert writes its rows in key order: two runs
recording `test_stats` for the same tests at once then lock the rows in the same order, and the
second waits instead of failing with `deadlock detected` (which 4,000 rows in opposite orders did
every time before; `tests/db/test_upsert.py`). Should a deadlock (SQLSTATE `40P01`) or a
serialization failure (`40001`) still happen, the orchestrator's short transactions
(`orchestrator/planner.run_transaction`) run again with backoff, as they do for SQLite's busy
error: asyncpg's errors reach SQLAlchemy as a plain `DBAPIError`, so the retry looks at the
SQLSTATE. A statement timeout (`57014`) is not retried (`tests/db/test_transaction_retry.py`, which
also provokes a real deadlock).

**Running them.** `coverset init-db` migrates and prints each revision and the backend.
`coverset serve` migrates before it opens the port (`COVERSET_MIGRATE_ON_START`,
[deploy.md](deploy.md#migrations-on-deploy)). On Postgres a migration takes the transaction-scoped advisory lock `coverset:migrate`, so replicas
that start together migrate one after the other, and the second finds the schema at head.
Checking whether a database is at head costs one read of `alembic_version`; the revision scripts are
loaded only when there is something to apply. For development, `uv run alembic upgrade head`,
`uv run alembic check` and `uv run alembic revision --autogenerate -m "..."` honour
`COVERSET_DATABASE_URL` too; generate revisions against SQLite and check them against both.

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
- On SQLite the lease is implicit (one process per database) and the dispatcher starts exactly as
  before.

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

## Backups

The provider's: Neon, Supabase and RDS keep point-in-time recovery (the retention depends on the
plan); Fly Postgres takes daily volume snapshots. Use `pg_dump` for an ad-hoc copy, or
`coverset db copy --from <postgres url> --to sqlite+aiosqlite:///backup.db` for a self-contained
file any coverset can open. The Modal module's 60 s SQLite backup loop is for SQLite only.

## Moving a SQLite instance to Postgres

`coverset db copy` copies every table, in dependency order, through SQLAlchemy Core with the
models' column types (so datetimes, JSON, booleans and digests are converted the way the app reads
and writes them), in batches (`--batch-size`, default 1,000), inside one transaction on the target,
then moves the target's sequences past the copied ids and compares every table's row count.

1. Bring the source to head: `uv run coverset init-db` (the copy refuses a source behind it).
2. Stop `coverset serve`: rows written during the copy would be missed.
3. Create an empty database on the provider. The copy migrates it to head and refuses a target that
   holds rows in any coverset table.
4. Copy:

   ```
   $ COVERSET_DATA_DIR=data uv run coverset db copy --to postgresql://coverset:coverset@127.0.0.1:55432/demo_copy
   copying sqlite+aiosqlite:////.../data/coverset.db -> postgresql+asyncpg://coverset:***@127.0.0.1:55432/demo_copy
   applied migration 0001 initial to the target
   ...
   applied migration 0010 an index on run_events.at for the retention gc to the target
     envs                        1 rows
     repos                       1 rows
     ...
     run_events              1,288 rows
     test_attempts             569 rows
     test_results              567 rows
     test_stats                108 rows
   copied 2,696 rows in 15 tables; row counts match the source
   ```

   That is the demo's database (8 runs), copied in 1.0 s including the migrations.
5. Set `COVERSET_DATABASE_URL` to the target, start, and check with `coverset db status` and
   `coverset deploy check`.
6. Facts, mirrors and uploads are files, not rows: move them with the object store
   ([object-store.md](object-store.md)) or keep the same data directory.

The copy works in every direction (Postgres to SQLite included). A self-reference
(`runs.scoring_basis_run_id`) is written after its table, so row order never matters. A value
SQLite accepted but Postgres does not (a string longer than its column, a NUL character) stops the
copy with the table's name and leaves the target empty.

Verified on the demo copy: 400 pages crawled from a `serve` on the copied Postgres database, all
200; a run queued with `coverset run --no-wait` was dispatched by the second replica after the first
was killed.

## Tests on Postgres

`COVERSET_TEST_DATABASE_URL` (an asyncpg URL whose user may `CREATE DATABASE`) switches the
`settings` fixture (`tests/conftest.py`) from a SQLite file to Postgres: each pytest-xdist worker
migrates one template database once, and every test gets `CREATE DATABASE <fresh> TEMPLATE
<template>` (about 50 ms), already at head, dropped (`WITH (FORCE)`) before the next test's.

```sh
make test-pg   # = make services, then COVERSET_TEST_DATABASE_URL=postgresql+asyncpg://coverset:coverset@127.0.0.1:55432/coverset uv run pytest -q -n 6
```

Measured at integration on 2026-10-14 (six workers, an M4 Max MacBook, the compose stack):

| Backends | Passed | Skipped | Failed | Time |
|---|---|---|---|---|
| SQLite and local files (the default) | 3,027 | 32 | 0 | 141 s |
| Postgres (`COVERSET_TEST_DATABASE_URL`) | 3,037 | 22 | 0 | 156 s |
| Postgres and MinIO (both variables) | 3,036 | 23 | 0 | 176 s |

The Postgres-only tests of `tests/db` run there instead of being skipped. Tests marked
`@pytest.mark.sqlite_only` (pragmas, batch table rebuilds, rowid reuse) are skipped under Postgres;
the rest were made backend-neutral instead: the data-directory privacy tests check the directories
on both backends and the database file's mode only where there is one, and the `skip_migrations`
test gets an empty Postgres database (`PostgresTestDatabases.create(empty=True)`) rather than the
migrated clone. The page and report tests' shared demos (`tests/web/conftest.py`,
`tests/report/conftest.py`) are seeded into a Postgres database of their own, so every page test
renders from Postgres too; tests read or doctor that database through
`coverset.db.testing.run_sql`, which runs on either backend (portable SQL: `false` for a boolean,
`RETURNING id`, `CAST(payload AS TEXT) LIKE ...` on a JSON column).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `TypeError: connect() got an unexpected keyword argument` | a driver parameter asyncpg does not take; `sslmode`, `connect_timeout` and `channel_binding` are handled, others (`options`, `gssencmode`) must go |
| `make services` fails with "Ports are not available ... 5432" | an older copy of the compose file; this one uses 55432 (or set `COVERSET_DEV_PG_PORT`) |
| a request fails with `canceling statement due to statement timeout` | a statement of the web process ran 30 s; find it in the server log, and run large one-off work from the CLI (no timeout) |
| `remaining connection slots are reserved` / `too many connections` | replicas × 20 plus CLI commands exceed the server's limit: fewer replicas, a bigger plan, or a session-mode pooler in front |
| no replica dispatches runs | `coverset db status` shows the lease holder; a holder that is wedged but connected keeps it: restart it. A holder whose dispatcher loop crashed logs `run dispatcher failed; it starts again in 5 s` and retries by itself |
| the lease stays with a replica whose host is gone | expected for up to about 2 minutes (TCP keepalives); longer means the server ignores the keepalive settings (a pooler in front, or a provider that fixes them): set the provider's `tcp_keepalives_*` or `idle_session_timeout`, or restart the dead host's successor |
| `prepared statement "__asyncpg_stmt_..." does not exist` | a transaction-mode pooler: use the direct URL ([Pooling](#pooling)) |
