# Deploying coverset

coverset's control plane (the web UI, `/webhooks/github`, the `/v1` API, the run dispatcher, the
reaper and the warm-pool maintainer) is one Python process, built from one image. Shards run
elsewhere: in Modal Sandboxes (`COVERSET_EXECUTOR=modal`) whichever host serves the control plane.

Its rows are always in Postgres (`COVERSET_DATABASE_URL`, [postgres.md](postgres.md)); its blobs
(facts, uploads, uploaded sources) are in one of two places:

* **Hosted: an S3-compatible bucket.** `COVERSET_BLOB_URL` names a bucket (AWS S3, MinIO,
  Cloudflare R2, Tigris). Nothing on the container's disk has to survive a restart: the data
  directory is a disposable cache. Any platform that runs a container can host it, a redeploy loses
  no run, fact or setting (the GitHub App `/setup` creates and the reaper's sandbox tag are database
  rows too), and a second replica can share the database (the leader lease keeps the background loops in one
  of them). **This is the shape for anything other people depend on.**
* **Files under `COVERSET_DATA_DIR`.** No bucket to run, which is what the demo, the tests and
  `coverset dev` use. It works on a server too, as long as the data directory is a disk that
  outlives the container.

`Settings.hosted` is true when a blob URL is set, and `coverset deploy check` says which shape a
deployment is in (the `state` row). The object store is described in
[object-store.md](object-store.md); this page is how to run them.

- [What the app needs](#what-the-app-needs)
- [Files or a bucket](#files-or-a-bucket)
- [Recipes](#recipes): [Fly.io](#flyio), [AWS (ECS Fargate, RDS, S3)](#aws-ecs-fargate-rds-s3),
  [one VM with compose](#one-vm-lightsail-or-ec2-with-compose), [Modal](#modal),
  [Railway and Render](#railway-and-render), [a demo from your laptop](#a-public-demo-from-your-laptop-ngrok)
- [Migrations on deploy](#migrations-on-deploy), [secrets](#secrets-and-rotation),
  [backups](#backups), [scaling](#scaling), [cost](#cost)
- [Smoke-test a hosted state](#smoke-test-a-hosted-state), [`coverset deploy check`](#coverset-deploy-check)
- [Docker image](#docker-image), [proxies, cookies and HSTS](#proxies-cookies-and-hsts),
  [the GitHub App](#setting-up-the-github-app-on-the-public-url), [the live Modal deployment](#the-live-modal-deployment),
  [troubleshooting](#troubleshooting)

Status words: **verified** means run on this machine on 2026-10-14 (against the
`deploy/dev-compose.yml` Postgres 17 + MinIO stack where it says so); **config only** means the
files and commands were written against the provider's documentation and not deployed (no account
was used tonight).

## What the app needs

| Need | Why | How |
|---|---|---|
| **One long-lived process, or a leader among replicas** | The run dispatcher, the retention gc, the sandbox reaper and the warm-pool maintainer run inside the web process, and webhooks must not wait for a cold start. A session advisory lock (the leader lease, `db/leader.py`) runs those loops in exactly one replica; the others serve requests. | Always on: `min_machines_running = 1` (Fly), a desired count of 1+ (ECS), `min_containers=1` (Modal). Not scale-to-zero, and not a platform that throttles the CPU between requests (AWS App Runner does; see the AWS recipe). |
| **An SSE-friendly proxy** | Run pages and `coverset-ci wait` stream events (`text/event-stream`); coverset sends `: ping` every 15 s. | No response buffering, an idle timeout well above 15 s (600 s here), no cap on a request's total duration below a few minutes. Caddy `flush_interval -1`; ALB idle timeout 600 s; Fly `idle_timeout = 600`. |
| **Postgres 14+** | Every row: runs, tests, results, events, tokens, settings. The tests run on 14 and 17. | A direct or session-mode connection string. Not a transaction-mode pooler (PgBouncer `pool_mode=transaction`, Neon's `-pooler` host, Supabase on 6543): the leader lease is a session lock and asyncpg prepares statements per connection. Each process opens a pool of 10 + 10 overflow (`coverset deploy check` prints it). The database is created on first start when the URL's user may `CREATE DATABASE`; otherwise create it once. |
| **An S3-compatible bucket** (hosted) | Facts (Parquet), run manifests, the per-repository dictionary, uploads and uploaded sources. | `COVERSET_BLOB_URL=s3://bucket[/prefix]?endpoint_url=...&region=...` (no `endpoint_url` for AWS S3). Credentials from `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE` or an instance/task role. The store must honour conditional writes (`If-Match`, `If-None-Match`); `coverset deploy check` probes it. |
| **A public `https` URL** | GitHub's webhooks, the App's callbacks, the OIDC audience of uploads, `Secure` cookies and HSTS all derive from `COVERSET_PUBLIC_URL`. | The platform's TLS (Fly, Modal, an ALB with ACM) or Caddy on a VM. A public URL requires `COVERSET_ADMIN_TOKEN` (32+ characters): open mode is refused. |
| **A disposable local cache** | DuckDB and pyarrow read local files, so facts are downloaded once into `COVERSET_CACHE_DIR`; git mirrors live there too. | On the container's own disk (`/var/cache/coverset` in the image); bounded by `COVERSET_CACHE_MAX_GB` (10 by default; gc and start-up evict beyond it). Lose it and the next request re-downloads. |
| **Secrets** | Admin token, secret key, the database URL, bucket credentials. | The platform's secret store, never the image. `COVERSET_SECRET_KEY` is required for `/setup`: the GitHub App it creates (ids, client secret, webhook secret, private key) is stored in the database, encrypted under a key derived from it ([security.md](security.md#5-secrets)), so the platform's secrets never need the App's keys. An operator who prefers to keep them there sets `COVERSET_GITHUB_APP_*` (the key as PEM text in `COVERSET_GITHUB_APP_PRIVATE_KEY`) and `COVERSET_GITHUB_WEBHOOK_SECRET` instead, and `/setup` is then read-only. |
| **A Modal token** (optional) | The executor creates sandboxes. | `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`; not needed on Modal itself, where the container runtime authenticates. Upload mode needs no executor at all. |

## Files or a bucket

| | Files under the data dir | Hosted: a bucket |
|---|---|---|
| Set with | `COVERSET_DATABASE_URL` | `COVERSET_DATABASE_URL` + `COVERSET_BLOB_URL` (+ bucket credentials) |
| Services to run | Postgres | Postgres and a bucket (managed, or the compose `hosted` profile on one VM) |
| What must survive a restart | the database and the data directory | the database and the bucket; the data dir and cache are disposable |
| Replicas | the ones that share the data directory's disk | one or more (the leader lease runs the background loops once) |
| Deploys | in place, on the disk that holds the data | anywhere: a new container is as good as the old one |
| Backups | the database's, and the data dir's disk | provider snapshots / PITR + bucket versioning |
| Good for | the demo, development, a public demo through ngrok, one person's server on one VM | anything a team or CI depends on; Fly, ECS, Modal, Railway, Render |
| Develop against it | `make dev` (the local Postgres) | `make dev-hosted` (Postgres 17 + MinIO in Docker, [dev.md](dev.md#hosted-like-development)) |

## Recipes

Every recipe below is stateless (Postgres + a bucket) except the VM without its `hosted` profile,
and runs the same image (`Dockerfile`; Modal builds the same contents natively).

| | **Fly.io** | **AWS: ECS Fargate** | **One VM + compose** | **Modal** | **Railway / Render** |
|---|---|---|---|---|---|
| Files | `deploy/fly/` | `deploy/aws/ecs-task-definition.json`, `iam-policy.json` | `deploy/compose.yml` (Postgres; MinIO with profile `hosted`), `deploy/aws/user-data.sh` | `deploy/modal/coverset_web.py` | the Dockerfile |
| Postgres | Fly Managed Postgres, or Neon | RDS for PostgreSQL | the `postgres` service on the VM | Neon, RDS, Supabase (direct), any | the platform's Postgres |
| Bucket | Tigris (`fly storage create`) | S3 with the task role | the `minio` service on the VM | R2, S3, Tigris | R2 or S3 |
| TLS and URL | `https://<app>.fly.dev` | ALB + ACM on your domain | Caddy + Let's Encrypt (`sslip.io` without a domain) | `https://<workspace>-coverset--coverset-web.<region>.modal.run` | the platform's |
| Status | config only | config only | the `hosted` profile **verified locally** 2026-10-14 with Caddy (`tls internal`); Postgres without the profile: config only | tested with `modal` stubbed (`tests/test_deploy_hosted.py`), not deployed in this shape | notes only |

### Fly.io

`deploy/fly/fly.toml` is one always-on Machine with **no volume**: Fly can replace it anywhere in
the region and a deploy loses nothing. The commands are in [deploy/fly/README.md](../deploy/fly/README.md);
in short:

```sh
fly launch --no-deploy --copy-config --config deploy/fly/fly.toml --dockerfile Dockerfile --name coverset-<you>
fly storage create --app coverset-<you> --name coverset-<you>-state   # Tigris; sets AWS_* + BUCKET_NAME secrets
fly mpg create --name coverset-<you>-db --region iad                # or a Neon project; copy the direct URL
fly secrets set --app coverset-<you> --stage \
  COVERSET_DATABASE_URL='postgresql://...' \
  COVERSET_BLOB_URL='s3://coverset-<you>-state?endpoint_url=https://fly.storage.tigris.dev&region=auto' \
  COVERSET_PUBLIC_URL=https://coverset-<you>.fly.dev \
  COVERSET_ADMIN_TOKEN="$(openssl rand -hex 32)" COVERSET_SECRET_KEY="$(openssl rand -hex 32)" \
  MODAL_TOKEN_ID=ak-... MODAL_TOKEN_SECRET=as-...
fly deploy --config deploy/fly/fly.toml --dockerfile Dockerfile --app coverset-<you>
fly ssh console --app coverset-<you> -C "coverset deploy check"
```

`fly storage create` stores `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`,
`AWS_REGION` and `BUCKET_NAME` as the app's secrets; coverset reads the first two, and the bucket and
endpoint go into `COVERSET_BLOB_URL`. Tigris documents conditional writes; the `object store` row of
`coverset deploy check` confirms them on the real bucket. Fly's `DATABASE_URL` (from `fly mpg
attach` or `fly postgres attach`) is not read: set `COVERSET_DATABASE_URL` to the same value (a
`postgres://` URL is normalised).

### AWS: ECS Fargate, RDS, S3

The production shape on AWS, with no long-lived credentials in the container:

1. **Image**: build and push to ECR (`docker build --platform linux/arm64 -t <account>.dkr.ecr.<region>.amazonaws.com/coverset .`).
2. **Postgres**: RDS for PostgreSQL 17, `db.t4g.micro` to start, in private subnets; a security
   group that admits 5432 from the service's security group only. The connection string goes into
   Secrets Manager as `coverset/database-url` (`postgresql://coverset:<password>@<endpoint>:5432/coverset`).
   Connect directly, not through RDS Proxy (it multiplexes connections between transactions
   unless a session gets pinned, which is the transaction-pooler problem below).
3. **Bucket**: a private S3 bucket (`coverset-prod`, Block Public Access on, versioning on with a
   lifecycle rule expiring noncurrent versions after 30 days ([Backups](#backups)), SSE-S3),
   `COVERSET_BLOB_URL=s3://coverset-prod/coverset?region=us-east-1`. S3 supports conditional writes
   (`If-None-Match` since August 2024, `If-Match` since November 2024).
4. **Task role**: [`deploy/aws/iam-policy.json`](../deploy/aws/iam-policy.json), least privilege:
   `s3:ListBucket` and `s3:GetBucketLocation` on the bucket, `s3:GetObject`, `s3:PutObject`,
   `s3:DeleteObject` and `s3:AbortMultipartUpload` on `coverset/*` only. Listing is granted on the
   whole bucket on purpose: without it S3 answers 403 instead of 404 for a missing key, and coverset
   treats "not found" and "forbidden" differently. Use a bucket of its own. Add `kms:Decrypt` and
   `kms:GenerateDataKey` on the key if the bucket uses SSE-KMS. The execution role needs
   `secretsmanager:GetSecretValue` on the `coverset/*` secrets.
5. **Task definition**: [`deploy/aws/ecs-task-definition.json`](../deploy/aws/ecs-task-definition.json)
   (0.5 vCPU, 1 GB, ARM64): plain settings in `environment`, every secret in `secrets` from Secrets
   Manager, the health check on `/healthz`, logs to CloudWatch. No `AWS_*` keys anywhere: boto3
   finds the task role's credentials itself. Replace the account id, region, image and URL.
6. **Service and load balancer**: an ECS service with a desired count of 1 (or 2, see
   [Scaling](#scaling)), `minimumHealthyPercent=100`, `maximumPercent=200`, behind an ALB with an ACM
   certificate. Target group health check `/healthz`; **ALB idle timeout 600 s** (the default 60 s
   is enough for the 15 s pings, but long silent stretches through other proxies are not);
   deregistration delay 30 s (serve drains for 10 s). `COVERSET_TRUST_PROXY=true`: the ALB appends
   the client to `X-Forwarded-For`.
7. **Check**: `aws ecs execute-command ... --command "coverset deploy check"` (needs ECS Exec), or
   run the task once with the command overridden to `deploy check`.

**AWS App Runner** is simpler to set up from the same image, but two of its properties work against
coverset: it throttles an instance's CPU when no request is in flight, which starves the in-process
dispatcher, reaper and gc, and it caps a request's duration (120 s), which cuts SSE streams. Use
Fargate.

### One VM (Lightsail or EC2) with compose

The cheapest always-on option keeps everything on one machine: [`deploy/compose.yml`](../deploy/compose.yml)
runs coverset, Postgres 17 and Caddy, and its **`hosted` profile** adds MinIO (built by Chainguard;
MinIO no longer publishes pullable `minio/minio` images) whose data sits on the VM's data disk.
coverset's own container is then disposable, exactly as on a platform.

```sh
cd deploy
cp coverset.env.example coverset.env && cp services.env.example services.env   # fill both, chmod 600
# coverset.env: COVERSET_DATABASE_URL=postgresql+asyncpg://coverset:<POSTGRES_PASSWORD>@postgres:5432/coverset
#   with the profile also COVERSET_BLOB_URL=s3://coverset?endpoint_url=http://minio:9000&region=us-east-1
#               AWS_ACCESS_KEY_ID=<MINIO_ROOT_USER>  AWS_SECRET_ACCESS_KEY=<MINIO_ROOT_PASSWORD>
COMPOSE_PROFILES=hosted COVERSET_DOMAIN=coverset.example.com docker compose up -d --build
```

[`deploy/aws/user-data.sh`](../deploy/aws/user-data.sh) does all of it on a fresh Ubuntu 24.04
instance: random passwords in `/opt/coverset/services.env`, the URLs appended to
`/opt/coverset/coverset.env`, Postgres (and, with `COVERSET_STATE=hosted`, MinIO) data under
`/srv/coverset/data/{postgres,minio}` on the attached disk, coverset's own data dir under
`/srv/coverset/data/app`, and a nightly `pg_dump` into `/srv/coverset/data/backups` (7 days). The
MinIO bucket is created with versioning on and a lifecycle rule that expires noncurrent versions
after 30 days (`COVERSET_MINIO_NONCURRENT_DAYS`), so what gc and purge delete leaves the shared
disk a month later instead of never. Neither service publishes a port; only Caddy does. Without the
profile (the default, `COVERSET_STATE=files`) the blobs are files in coverset's data dir on the data
disk. A 2 GB instance is tight for three services plus the image build: take 4 GB (Lightsail $24)
for the `hosted` profile.

Verified on this machine (2026-10-14, Docker Desktop, project name `coverset-pass-c`, Caddy with
`tls internal` on ports 18088/18443): `COMPOSE_PROFILES=hosted docker compose up -d --build` started
Postgres, MinIO, the one-shot `minio-init` ("Bucket created successfully", "versioning is enabled",
exit 0), then coverset once Postgres was healthy, then Caddy; `https://coverset.localhost:18443/healthz`
answered through Caddy, and `coverset deploy check` in the container passed the `database`
(Postgres 17.11 at head 0009), `object store` (conditional writes ok), `cache dir` and `state`
(`hosted`) rows. The compose file's Postgres-without-the-profile layout and the `user-data.sh`
changes pass `docker compose config` and `bash -n`; they were not run on a VM.

Operating it is unchanged ([deploy/aws/README.md](../deploy/aws/README.md)): `docker compose
--env-file /etc/coverset.compose.env ps|logs|exec`. psql: `docker compose --env-file
/etc/coverset.compose.env exec postgres psql -U coverset coverset`.

### Modal

`deploy/modal/coverset_web.py` serves the app from an always-on Modal container that keeps no
state: the Secret `coverset-web` must hold a Postgres `COVERSET_DATABASE_URL`, an `s3://`
`COVERSET_BLOB_URL`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` for the bucket,
`COVERSET_ADMIN_TOKEN` and `COVERSET_SECRET_KEY`, or the container refuses to start with one line
naming what is missing. The data dir, the cache and the working directory are on the container's
disk (`/root/coverset-data`); the GitHub App `/setup` creates is stored in the database, so the
Secret carries no App keys (unless you choose to manage them there). `COVERSET_WEB_MAX_CONTAINERS=2` (deploy time) lets Modal run a second container; the leader
lease keeps the background loops in one of them.

```sh
cat > /tmp/coverset-web.env <<'EOF'
COVERSET_ADMIN_TOKEN=<64 hex>
COVERSET_SECRET_KEY=<64 hex>
COVERSET_PUBLIC_URL=https://<workspace>-coverset--coverset-web.<region>.modal.run
COVERSET_DATABASE_URL=postgresql://<user>:<password>@<host>/<db>?sslmode=require
COVERSET_BLOB_URL=s3://<bucket>/coverset?endpoint_url=https://<account>.r2.cloudflarestorage.com&region=auto
AWS_ACCESS_KEY_ID=<bucket key>
AWS_SECRET_ACCESS_KEY=<bucket secret>
EOF
uv run modal secret create coverset-web --force --from-dotenv /tmp/coverset-web.env -e coverset && rm /tmp/coverset-web.env
uv run modal deploy --strategy recreate -e coverset deploy/modal/coverset_web.py
uv run modal container exec --no-pty <id> -- sh -c 'cd /root/coverset-data && coverset deploy check'
```

The image's environment carries the container's layout (`COVERSET_DATA_DIR` and `COVERSET_CACHE_DIR`
under `/root/coverset-data`, `COVERSET_EXECUTOR=modal`, `COVERSET_ENV=prod`,
`COVERSET_TRUST_PROXY=false`), so an exec shell checks what the server uses. A restart loses no run,
fact, setting, GitHub App or sandbox tag: all of them are in the database or the bucket.

### Railway and Render

Both deploy the Dockerfile from the repository and run it always on. Add the platform's managed
Postgres and set `COVERSET_DATABASE_URL` from its connection string (both hand out a `postgres://`
URL, which is normalised; use the internal hostname, and Render's or Railway's direct URL rather
than a pooled one). Neither has an S3-compatible bucket of its own that coverset has been tried
with: use Cloudflare R2 (no egress fees; `endpoint_url=https://<account>.r2.cloudflarestorage.com`,
`region=auto`) or S3, with an access key scoped to that bucket. Set `COVERSET_PUBLIC_URL` to the
service URL, `COVERSET_TRUST_PROXY=true`, the admin token and secret key, and keep one instance
(or more). Render's free web services sleep when idle, which does not fit (webhooks
wait, the dispatcher stops); use a paid instance.

### A public demo from your laptop (ngrok)

For a demo, the laptop is enough: run coverset on your machine and let a tunnel give it a
public `https` URL. coverset prints the recipe and does not start the tunnel itself:

```sh
uv run coverset dev --tunnel            # prints the ngrok/cloudflared command and the two settings
ngrok http http://127.0.0.1:8000        # another terminal; prints https://<name>.ngrok-free.app
export COVERSET_PUBLIC_URL=https://<name>.ngrok-free.app
export COVERSET_ADMIN_TOKEN=<the 64 hex characters coverset dev --tunnel printed>
uv run coverset dev --tunnel            # now serves, and prints `ngrok http --url=https://<name>... `
```

The two settings are required: a public URL refuses open mode (every visitor would be the
operator), and links, cookies and the GitHub App's webhook URL all come from the public URL.
`COVERSET_TRUST_PROXY=true` is optional: the tunnel appends the visitor's address to
`X-Forwarded-For`, so rate limits apply per visitor instead of to the tunnel. Sign in at
`<url>/login` with the token. A GitHub App created at `<url>/setup` on that URL receives webhooks
through the tunnel; with a free ngrok domain (a stable name per account) the App keeps working
across restarts, with a quick `trycloudflare.com` tunnel it does not (a new name each time).
ngrok's free tier shows browsers an interstitial page before the first request; webhooks and API
clients are not affected. `make dev-hosted` combines with this (Postgres + MinIO behind the same
tunnel). [dev.md](dev.md#a-public-demo-through-a-tunnel) has the details.

## Migrations on deploy

`coverset serve` migrates the database to head **before it opens the port**
(`COVERSET_MIGRATE_ON_START=true`, the default): it runs `coverset init-db`, prints each applied
revision, and exits non-zero if a migration fails, so the platform sees a failed start instead of a
half-migrated app answering its health check. The app's own startup migration is then skipped. So
the first boot of a container against an empty database creates the schema, and every deploy that
ships a new revision applies it.

With **two or more replicas** starting together, each migrates: the migration takes a
transaction-scoped advisory lock (`coverset:migrate`), so they run one after the other and the
second finds the schema at head. To migrate once per deploy instead, set
`COVERSET_MIGRATE_ON_START=false` on the service and run `coverset init-db` in a release step: Fly's
`release_command = "init-db"` (commented in `fly.toml`), an ECS one-off task with the command
`init-db` before updating the service, or `docker compose run --rm coverset init-db` on a VM. During
a rolling deploy the old version briefly runs against the new schema, so a revision must keep the
previous release working (add columns before using them; drop them a release later).
`COVERSET_SKIP_MIGRATIONS=true` still means "never migrate" (tests). The first start also creates
the database when the server has none of its name and the URL's user may `CREATE DATABASE`
([postgres.md](postgres.md#the-database-coverset-uses)); a server that does not answer stops the
start with exit status 2.

## Secrets and rotation

| Secret | Where it lives | Rotate |
|---|---|---|
| admin token (`COVERSET_ADMIN_TOKEN`) | platform secrets | set the new value, restart (Fly: `fly secrets set`, restarts; ECS: new secret version, force a new deployment; Modal: recreate the Secret, `modal deploy --strategy recreate`; VM: edit the env file, `docker compose up -d`). Signs every browser out. |
| secret key (`COVERSET_SECRET_KEY`) | platform secrets | it also encrypts the stored GitHub App, so re-encrypt first: `COVERSET_SECRET_KEY=<new> coverset secrets rekey --old-key <old>` (one transaction; [security.md](security.md#5-secrets)), then give every replica the new value and restart them as above, back to back: a replica still on the old key cannot open the App, and a `/setup` run there would seal a new App under the old key, so use `/setup` only after the rollout. Signs every browser out. |
| database password | the URL in `COVERSET_DATABASE_URL` | create the new password (RDS: a second user or a password change during a deploy window), update the secret, restart the replicas, then revoke the old one |
| bucket credentials | `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, or none with a role | create a second access key, update the secret, restart, delete the old key. With a task or instance role there is nothing to rotate. |
| GitHub App credentials | the database (stored by `/setup`, encrypted under `COVERSET_SECRET_KEY`); or `COVERSET_GITHUB_APP_*` + `COVERSET_GITHUB_WEBHOOK_SECRET` in platform secrets when you manage them | stored by `/setup`: replace the App (`/setup` again, signed in with the admin token; it asks before overwriting) or **Forget the App** there first; there is no command to swap the key of the stored App in place. In platform secrets: generate a new key in the App's settings, replace the secret, restart, delete the old key on GitHub |
| Modal token | `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` (none on Modal) | create a new token, update, restart, revoke the old one |

`modal secret create --force` replaces the whole Secret, so keep its file (or your password manager)
as the source of truth. Losing `COVERSET_SECRET_KEY` loses the stored GitHub App (nothing else can
open it): keep the key with the other secrets, and after a loss forget the App on `/setup` and
create it again.

## Backups

What to back up is exactly what is not disposable: the database and the blobs (the bucket, or the
data directory's `facts/`, `sources/` and `uploads/` without one). The cache and the mirrors are
rebuilt on demand.

* **Postgres**: the provider's automated backups with point-in-time recovery (RDS: a retention
  period of 7+ days; Fly Managed Postgres and Neon keep their own history). On the VM, the nightly
  `pg_dump` plus the data disk's snapshots. Restore into a new database and point
  `COVERSET_DATABASE_URL` at it.
* **Bucket**: turn **object versioning** on (S3, R2, Tigris and MinIO support it), plus a
  lifecycle rule that expires noncurrent versions after 30 days. Without the rule a versioned bucket
  keeps every object gc, `repo purge` or a manifest rewrite deletes, so retention frees nothing. The
  VM's `minio-init` sets both (its rule's age is `COVERSET_MINIO_NONCURRENT_DAYS`); on S3, R2 and
  Tigris add the rule yourself (S3: a lifecycle rule with "permanently delete noncurrent versions").
  Facts are written once and never updated in place, so versioning costs little: the run manifests
  and the dictionary are the only objects rewritten, and their previous versions are what a
  restore needs. Optionally replicate the bucket to another region.
* **Consistency**: the database is the index of the bucket (runs point at facts). Restore both to
  roughly the same moment; facts newer than the database are ignored and later removed by gc, rows
  whose facts are missing show the run without its coverage.
* **Files on a server**: the data disk's snapshots, taken after the nightly `pg_dump` so the two
  match.

## Scaling

One replica handles the demo's load with room to spare; add a second for zero-downtime deploys
and to survive a host failure, not for throughput.

* **Replicas share Postgres and the bucket.** The leader lease (a Postgres session advisory lock)
  runs the dispatcher, gc, reaper and warm-pool maintainer in exactly one replica; another takes
  over within about 30 s of the holder disappearing (17 s in the two-container check above).
  `coverset deploy check` names the holder in its `database` row. Any replica may queue a run; the leader picks it up within
  two seconds. Upload processing runs in the replica that received the upload.
* **Conditional writes** keep replicas from overwriting each other's updates to the dictionary and
  run manifests; a store that ignores `If-Match` makes `coverset deploy check` warn, and then only
  one replica is safe.
* **Each replica has its own cache** (`COVERSET_CACHE_DIR`), filled on first read.
* **Sandbox reaping spans containers.** With the Modal executor every sandbox is tagged with the
  installation's id, the `instance_id` row of the database's `settings` table (created once, the
  first time any process needs it), so a new container (a restart, a redeploy, a leader failover)
  and `coverset modal reap` both find the sandboxes an earlier container left behind, warm-pool
  ones included. `coverset modal reap --any-instance` still reaches other installations' (a
  laptop's, a benchmark's).
* **Rate limits are per replica** (in memory): with two replicas a client gets up to twice the
  configured rate.
* **Connections**: each replica opens up to 20 connections to Postgres (pool 10 + 10 overflow) plus
  the leader's one; a `db.t4g.micro` allows about 100, so a few replicas fit.

## Cost

Approximate monthly list prices for the control plane alone (shards on Modal are billed per run;
see [modal.md](modal.md)), from the providers' published pricing as known in 2026; check the
provider before relying on them.

| | Compute | Postgres | Bucket | Other | About |
|---|---|---|---|---|---|
| Fly.io | shared-cpu-1x, 1 GB: $6-7 | Fly Managed Postgres basic from ~$38, or Neon from $0-19 | Tigris: $0.02/GB-month, no egress fees: cents | | $10-45 |
| AWS Fargate | 0.5 vCPU + 1 GB ARM: ~$15 | RDS db.t4g.micro + 20 GB: ~$15 | S3: cents | ALB ~$18, Secrets Manager $2 | ~$50 |
| One VM (Lightsail) | 4 GB instance: $24 | on the VM | MinIO on the VM | snapshots ~$1-3 | ~$25 |
| One VM, files | 2 GB instance: $12 | on the VM | files | | ~$12 |
| Modal | 0.5 core + 1 GiB reserved: ~$23 (inside the Starter plan's credit) | Neon or another provider | R2: $0.015/GB-month, no egress fees | | $23-45 |
| Railway / Render | from ~$5-7 | from ~$6-7 | R2 or S3: cents | | $15-30 |

A scale-to-zero Postgres (Neon's free and usage plans) does not get to sleep: the leader lease keeps
one connection open, so budget for an always-on compute.

## Smoke-test a hosted state

`scripts/smoke_hosted.py` checks a real Postgres and bucket end to end before anyone depends on
them: it migrates, seeds the demo repository and runs its history and both PRs with the local
executor, starts `coverset serve` on a free port, fetches the dashboard, a PR page, a source view and
the Economy page with the admin token, uploads a bundle through `POST /v1/bundles` with a
repository token, lists the run's keys in the bucket and reads its rows back, then prints a table.

```sh
COVERSET_DATABASE_URL=postgresql://... COVERSET_BLOB_URL='s3://...' AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
  uv run python scripts/smoke_hosted.py          # leaves the demo in place to look at
uv run python scripts/smoke_hosted.py --clean-only   # (same environment) deletes it again
```

Every child gets `COVERSET_EXECUTOR=local` and `COVERSET_FORGE=fake`, so it creates no sandbox and
posts nothing to GitHub. It also gets a data directory of its own, never the laptop's `data/` (or a
`COVERSET_DATA_DIR`/`COVERSET_CACHE_DIR` from the shell or `.env`): `--data-dir`, by default
`data/smoke-hosted/<digest of the database and bucket>`, with the cache inside it. That matters
because the demo's git repository lives there and the database records its absolute path, and the
cache is keyed by repository id, which every new database starts at 1 again. The first row (`data
dir`) claims the directory with a `.coverset-smoke.json` marker; it refuses a directory holding
other files, and a database that already holds `coverset-demo/ledger` the smoke did not make (the
demo would repoint that repository at the smoke's git repository). `--clean`/`--clean-only` purge
the repository and delete the data directory only when the marker for the same target is there,
so they never touch a demo someone else seeded. Point it at a database of its own, or at a new
production database before anyone uses it. `repo purge` keeps the upload's audit-log entry (by
design), the demo's webhook-delivery log row and the environment cache row, so drop a database
used only for the smoke afterwards.

On 2026-10-14 on this machine it passed every row against the dev
stack (Postgres 17.11 + MinIO, a database and a key prefix of its own) in 33 s: the upload's run
had 22 keys in the bucket (21 Parquet files and `MANIFEST.json`) and 99 test results in Postgres;
`--clean-only` then left the prefix empty. A rerun with the `data dir` row passed 12 of 12 from a
directory whose `data/` held a stand-in laptop demo, which it left byte for byte, and a second
directory pointed at the same database was refused at `data dir`. Without `COVERSET_BLOB_URL` it
runs the same rows with the blobs as files in its data directory (`state` is then a WARN: not
hosted); `tests/test_smoke_hosted.py` runs it that way, on a database of each test's own.

Rerun at integration (2026-10-14, after the Postgres, object-store and dev passes had landed, head
`0010`), against a new database `coverset_smoke_int` and the prefix `s3://coverset/smoke-int`: 12 of
12 in 28 s (migrate 10 revisions 0.9 s, demo 20 s, serve ready 0.7 s, four pages 0.06-0.17 s each,
upload to verdict 5.5 s, 22 keys in the bucket, 99 test results and 3 events in Postgres).
`coverset deploy check --offline` in the same environment then reported `database` PASS
("Postgres 17.11, migrated to head 0010; 10 + 10 overflow connections per process"), `object
store` PASS (the conditional-write probe in 91 ms) and `state` PASS (hosted); its warnings were the
laptop's (dev environment, local public URL, no admin token, a secret-key file, the 10 GB cache
bound above the free disk, the local executor, no GitHub App or webhook secret). `--clean-only`
emptied the prefix; the database was dropped afterwards.

## `coverset deploy check`

Prints one `PASS`/`WARN`/`FAIL` row per readiness item and exits 1 on any `FAIL`: environment,
bind, public URL (https?), a `GET <public url>/healthz` probe (`--offline` skips it), admin token
(length only), secret-key source, proxy trust (aware of Modal), data dir writable and free disk,
then the state rows:

| Row | Says |
|---|---|
| `database` | the server version, the migrated revision against the packaged head, the connection pool and who holds the background-task lease; fails when it cannot connect or the database does not exist yet (it never creates one), warns for a transaction-mode pooler host or a missing migration (`coverset.db.session.check_database`) |
| `object store` | the blob store, local files or a bucket, probed with one small object under `_coverset/probe/`: create with `If-None-Match`, a write with a wrong `If-Match` that must be refused, read back, list, delete (`coverset.storage.check_store`); warns when conditional writes are ignored |
| `cache dir` | writable, its size against `COVERSET_CACHE_MAX_GB`, free disk under it; warns when the bound is larger than the disk |
| `state` | `hosted` (a bucket: nothing under the data dir must survive), `local` (blobs as files under the data dir on a private URL), or a warning for files behind a public URL (that directory must be a disk that survives restarts) |

then the executor and where the Modal client's credentials come from, the GitHub App (from the
environment, or the one `/setup` stored in the database, opened with `COVERSET_SECRET_KEY` as the
server would; a stored App that does not open is a failing row) and its client credentials,
webhook secret. `--json` prints one `{"name", "status", "detail"}` object per row.

## Docker image

`Dockerfile` (multi-stage, `python:3.13-slim`, uv 0.10.7 from PyPI, `uv sync --frozen --no-dev`,
git and the CA certificates, tini, non-root uid 10001, `HEALTHCHECK` on `/healthz`). Its
entrypoint is `tini -- coverset`, with `serve --bind 0.0.0.0:8000` as the default arguments (so
`docker run coverset init-db` runs `coverset init-db`); nothing is written at start. `COVERSET_DATA_DIR=/data` is a volume and the working directory (without
a bucket the blobs live there); the cache is `COVERSET_CACHE_DIR=/var/cache/coverset` on the
container's own disk; `COVERSET_MIGRATE_ON_START=true`. 1.09 GB; a cold build takes about 2 min 45 s,
a source-only rebuild seconds. The project is installed editable on purpose (the executors find
`packages/pytest-coverset` and `uv.lock` relative to `src/coverset`), and `uv` stays in the image
(plugin wheel builds, `uv export` of targets).

```sh
docker build -t coverset .
docker run -d --name coverset -p 8000:8000 -v coverset-data:/data \
  -e COVERSET_DATABASE_URL=postgresql://<user>:<password>@host.docker.internal:5432/coverset \
  -e COVERSET_ADMIN_TOKEN="$(openssl rand -hex 32)" -e COVERSET_PUBLIC_URL=http://localhost:8000 coverset
curl localhost:8000/healthz
docker exec coverset coverset deploy check --offline
```

Verified on 2026-10-14 with this image (built in about 3 min) against the dev stack's Postgres and
MinIO (`host.docker.internal`, no volume for `/data`): the container printed nine `applied
migration` lines and `database ready` before `serving on`, and `coverset
deploy check --offline` inside it gave 11 PASS and 4 WARN (public URL local, local executor, no
GitHub App, no webhook secret; `state`: hosted). After `docker restart` it said "already at the
latest revision" and served again. A second container on the same database migrated nothing,
served, and left the leader lease to the first (the lease query named the holder); 17 s after
the first was stopped, the second logged "leader lease coverset:background acquired". (That run
predates the vault: the image's entrypoint then still wrote `COVERSET_GITHUB_APP_PRIVATE_KEY` to a
key file; the image now runs `coverset` directly and was not rebuilt for this change.) The
migrate-at-start path is also covered by `tests/test_deploy_hosted.py`.

## Proxies, cookies and HSTS

* `COVERSET_TRUST_PROXY=true` installs `coverset.web.proxy.ProxyHeadersMiddleware`: the client
  address comes from the **last** `X-Forwarded-For` value (the one the nearest proxy appended;
  `Fly-Client-IP` on a Fly Machine) and the scheme from the last `X-Forwarded-Proto`. Values that
  are not IP addresses or `http`/`https` are ignored; `X-Forwarded-Host` is never honoured. Only
  set it when every request comes through a proxy that sets those headers. The headers stay visible,
  so the `/setup` "local operator" path stays closed behind a proxy (sign in instead).
* `COVERSET_SECRET_KEY` (32+ characters) makes the cookie key independent of the data directory
  and is the key the stored GitHub App is encrypted under; set it on every hosted deployment
  (without it the cookie key derives from the admin token, which is fine, or from a key file under
  the data dir, which is not: it changes with every container; and `/setup` cannot store an App).
  The admin token is still mixed into the cookie key, so rotating either signs everyone out.
* With an `https://` `COVERSET_PUBLIC_URL` every response carries
  `Strict-Transport-Security: max-age=31536000` and cookies are `Secure`.

## Setting up the GitHub App on the public URL

1. `coverset deploy check` inside the deployment should show only the GitHub rows as warnings.
   Modal: `uv run modal container list -e coverset`, then
   `uv run modal container exec --no-pty <id> -- sh -c 'cd /root/coverset-data && coverset deploy check'`; Fly:
   `fly ssh console -C "coverset deploy check"`; VM: `docker compose exec coverset coverset deploy check`.
2. Sign in at `<url>/login` with the admin token. The `/setup` callback accepts an admin session
   behind a proxy (the "local operator" path needs a loopback connection without proxy headers).
3. Open `<url>/setup` (or `/setup?org=<org>`), press **Create the GitHub App**, confirm on GitHub.
   The manifest uses `COVERSET_PUBLIC_URL` for the webhook, the setup redirect and the sign-in
   callback (`/auth/github/callback`), so the public URL must be final before this step. The button
   is disabled, with the reason, while `COVERSET_SECRET_KEY` is unset or the environment holds
   GitHub credentials.
4. coverset exchanges the code and stores the App (ids, client secret, webhook secret, private key)
   in the database, encrypted under `COVERSET_SECRET_KEY`. The success page shows the App's name,
   the installation link and "Credentials stored". Every replica picks the App up within about 5 s
   (no restart, no file to copy); with no `COVERSET_ALLOWED_OWNERS` only the App's owner is
   accepted.
5. Install the App on the repositories (the success page links to it). Pushes and pull requests
   now arrive at `/webhooks/github`; runs go to Modal.

[github.md](github.md) has the details of the flow, token mode and the Actions step.

## The live Modal deployment

* **URL:** `https://boydgreenfield-coverset--coverset-web.us-east.modal.run`
* **Admin token:** `~/.config/coverset/modal-web-admin-token` on the machine it was deployed from
  (mode 600). Sign in at `/login`, or `curl -H "Authorization: Bearer $(cat ~/.config/coverset/modal-web-admin-token)" <url>/`.
* **Stop it** (stops billing; the Secret stays): `uv run modal app stop coverset-web -e coverset`
* **Start / upgrade it:** `uv run modal deploy --strategy recreate -e coverset deploy/modal/coverset_web.py`
  (the Secret must name Postgres and a bucket, [above](#modal)).

| Object | Name (environment `coverset`) | Holds |
|---|---|---|
| App | `coverset-web` | one function, `web`: `@modal.asgi_app`, `min_containers=1`, `max_containers` from `COVERSET_WEB_MAX_CONTAINERS` (1 by default), `@modal.concurrent(max_inputs=256)`, 24 h per-request timeout |
| Secret | `coverset-web` | `COVERSET_ADMIN_TOKEN`, `COVERSET_SECRET_KEY`, `COVERSET_PUBLIC_URL`, `COVERSET_DATABASE_URL`, `COVERSET_BLOB_URL`, the bucket keys |
| App | `coverset-runner` (existing) | the shard sandboxes the web container creates |

The image is `debian_slim` (Python 3.13) + git + uv 0.10.7 + `uv sync --frozen --no-dev` of the
project (editable, as the Dockerfile does), with `.dockerignore` reused as the upload filter; the
plugin wheel is pre-built into the image. Deploy-time knobs: `COVERSET_WEB_CPU` (default 0.5 cores),
`COVERSET_WEB_MEMORY_MIB` (1024) and `COVERSET_WEB_MAX_CONTAINERS` (1).

The reaper's sandbox tag is the `instance_id` row of the database's `settings` table, created by the
first executor that needs it with `INSERT ... ON CONFLICT DO NOTHING` and read back, so concurrent
containers agree on one id and every later container reuses it (`executors/modal_.py`,
`instance_id`). Nothing about it is on the container's disk.

### Redeploys with one container must use `--strategy recreate`

With the default rolling strategy the new version waits for a container slot, and with
`max_containers=1` the healthy old container never frees it: on 2026-10-13 the URL answered nothing
for 25 minutes ("waiting to be scheduled on a CPU worker") until the old container was stopped by
hand (`modal container stop -y <id>`), after which the new one served in 14 s. `recreate` stops the
old container first. With `COVERSET_WEB_MAX_CONTAINERS=2` a rolling deploy works too.

### Measured on 2026-10-13 (modal 1.6.1, us-east)

| What | Result |
|---|---|
| `GET /healthz` over the public URL, from a laptop on the US east coast | 0.15-0.17 s |
| dashboard with a bearer token | 0.34 s |
| `/login` (form, cookie `Secure`, redirect to `https://`), `/setup` with the token | 200 |
| `POST /webhooks/github` with no secret configured | 503 `webhook_secret_unset` (fails closed) |
| ASGI lifespan | Quart's `before_serving` runs at container start (migrations, dispatcher, gc, reaper, pool maintainer); `after_serving` runs on stop ("warm pool drained") |
| a real run queued inside the container (`coverset run --repo <demo app> --head main --executor modal --no-wait`) | the dispatcher built the env image (11.2 s, first time), ran 97 tests in Modal sandboxes, **PASS**; its `/v1/runs/<id>/events` SSE stream delivered 97 `test` events and `done` in 26.6 s |
| SSE through Modal's gateway | a 210 s stream with an event every 15 s and a 330 s stream with 110 s of silence between events both completed with HTTP 200; coverset's own run-page stream (replay at 0.07x) stayed open 216 s and ended with `done` |
| request headers seen by the app | scope client = the caller's address, scheme `https`, `Via: 2.0 Caddy`; a client-sent `X-Forwarded-For: 6.6.6.6` was not passed through |
| redeploy (code change) | image layers after the dependency sync rebuild in ~10 s; deploy 31-54 s; a `--strategy recreate` deploy answered `/healthz` again 23 s after the deploy command returned |
| admin token rotation (new Secret, then a recreate deploy) | old token 401, new token 200, the earlier run still there |
| where it runs | the container reported `MODAL_REGION=eu-west` while the URL's gateway is `us-east`; no region is pinned (`region=` on the function would pin it; Modal prices region selection separately) |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `COVERSET_DATABASE_URL must be a Postgres URL for the asyncpg driver` | the URL names another database or driver (`sqlite+...`, `postgresql+psycopg://`): use `postgresql://` (normalised) or `postgresql+asyncpg://` |
| `cannot reach Postgres at ...` (exit 2) at start | the server is down or not reachable from the container: check the host, port and network; `deploy check` shows the same in its `database` row |
| `deploy check` database row: `looks like a transaction-mode pooler`, or random `prepared statement ... does not exist` / lost leader lease | a PgBouncer in `pool_mode=transaction` (Neon's `-pooler` host, Supabase's port 6543, RDS Proxy): use the direct or session-mode connection string |
| `deploy check` database row: `cannot connect: ... ssl` | the provider requires TLS: keep its `?sslmode=require` in the URL (it becomes asyncpg's `ssl`); `channel_binding` is dropped |
| `deploy check` object store row warns `ignores If-Match` | the store does not implement conditional writes (some older MinIO releases, some S3 look-alikes): run one replica, or use a store that does (S3, R2, Tigris, current MinIO) |
| object store row fails with `RequestTimeTooSkewed` or `SignatureDoesNotMatch` right after a container starts | the container's clock is off (SigV4 allows 15 minutes): fix NTP on the host; on a laptop, Docker Desktop's VM clock drifts after sleep (restart Docker). A wrong secret key gives `SignatureDoesNotMatch` too: check `AWS_SECRET_ACCESS_KEY` |
| object store row fails with `PermanentRedirect`, `AuthorizationHeaderMalformed ... region` or a 301 | the bucket lives in another region than `region=` says: set the bucket's region in `COVERSET_BLOB_URL` (R2 and Tigris take `auto`) |
| object store row fails with `AccessDenied` | the role or key lacks an action of `deploy/aws/iam-policy.json`, or the prefix in the URL is not the one the policy grants (`coverset/*`) |
| `NoCredentialsError` / `Unable to locate credentials` | no `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE` or role in the container: on Fly they come from `fly storage create`, on ECS from the task role, elsewhere set them |
| `/setup` says "COVERSET_SECRET_KEY is not set" (503) | the vault has no key: set `COVERSET_SECRET_KEY` (the same on every replica), restart, start `/setup` again; delete the App GitHub created meanwhile (its settings page) |
| `/setup` says "GitHub credentials come from the environment" (409) | a `COVERSET_GITHUB_APP_*`, `COVERSET_GITHUB_TOKEN` or `COVERSET_GITHUB_WEBHOOK_SECRET` is set: remove them all (and restart) to let `/setup` store the App, or keep managing them yourself |
| log line `the stored GitHub App is not usable: ... does not open with COVERSET_SECRET_KEY` | the key changed without a rekey: run `coverset secrets rekey --old-key <previous key>` with the new key in the environment, or forget the App on `/setup` and create it again |
| the first boot takes long or the health check fails during it | migrations run before the port opens: give the health check a grace period of 60 s for an empty database (`fly.toml`, the ECS `startPeriod`) |
| two replicas, and `coverset deploy check` shows no lease holder | the lease holder lost its connection; another replica takes over within about 30 s. With a transaction-mode pooler it never holds: see above |
| Modal: `RuntimeError: coverset-web needs in the Modal Secret 'coverset-web': ...` | the Secret lacks one of the state's settings it names: recreate the Secret with them ([Modal](#modal)), redeploy |
| Modal URL times out after a deploy, logs say "waiting to be scheduled on a CPU worker" | a rolling deploy deadlocked on `max_containers=1`: `modal container list -e coverset`, then `modal container stop -y <old id>`; deploy with `--strategy recreate` |
| Modal: every link is `http://` / login loops | `COVERSET_PUBLIC_URL` does not match the URL Modal printed (it contains the region, e.g. `.us-east.modal.run`) |
| `curl` to the container dies with exit 132, or Python dies with "Illegal instruction" on import of `cryptography` | Docker Desktop on Apple M4: OpenSSL's ARM feature probe hits SIGILL in the VM. Run with `-e OPENSSL_armcap=0` locally; not needed on x86 or Graviton servers |
| `docker build`/`pull` hangs at "docker-credential-desktop get" | the macOS keychain prompt cannot be answered from a script: run from a terminal, or with `DOCKER_CONFIG` pointing at a config without `credsStore` |
| webhooks answer 503 `webhook_secret_unset` | no GitHub webhook secret yet: finish `/setup` (taken up within seconds), or set `COVERSET_GITHUB_WEBHOOK_SECRET` and restart |
| webhooks answer 403 | signature mismatch: the App's webhook secret and coverset's differ; redeliver from the App's Advanced tab after fixing |
| GitHub says the webhook URL is invalid | it must be the public `https://` URL, not loopback; check `COVERSET_PUBLIC_URL` |
| `/setup/github/callback` answers 403 "Not allowed" | not signed in: sign in with the admin token first, then restart the flow at `/setup` |
| everyone shares one rate limit / login lockout | the proxy's address is the client: set `COVERSET_TRUST_PROXY=true` behind Caddy/Fly/an ALB/a tunnel (not on Modal) |
| `coverset deploy check` FAIL executor | no Modal credentials: `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` (Fly, ECS, VM) |
| a run stays queued | `COVERSET_PAUSED=true`, the daily budget is spent (`COVERSET_MODAL_DAILY_BUDGET_USD`), or `COVERSET_MAX_CONCURRENT_RUNS` are busy; see the run page |
