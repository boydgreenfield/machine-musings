# coverset on Fly.io

One always-on Machine (`shared-cpu-1x`, 1 GB) with **no volume**: the state is in Postgres (Fly
Managed Postgres, or Neon) and a Tigris bucket, so Fly can replace the Machine anywhere in the
region and a deploy loses nothing. TLS and a `https://<app>.fly.dev` URL come from Fly's proxy.
Shards still run on Modal; the Machine only runs the control plane. **Config only**: the commands
below follow Fly's documentation (October 2026) and were not run (no Fly account was used). See
[docs/deploy.md](../../docs/deploy.md) for what the app needs, the other platforms and costs.

Run everything from the **project root** (the Docker build context).

```sh
# 0. flyctl: https://fly.io/docs/flyctl/install/ ; then
fly auth login

# 1. Create the app from this config without deploying (pick a name; it rewrites `app` in the copy).
fly launch --no-deploy --copy-config --config deploy/fly/fly.toml --dockerfile Dockerfile --name coverset-<you>
APP=coverset-<you>

# 2. The bucket: Tigris, Fly's S3-compatible object storage. This sets AWS_ACCESS_KEY_ID,
#    AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL_S3, AWS_REGION and BUCKET_NAME as the app's secrets.
fly storage create --app "$APP" --name "$APP-state"

# 3. Postgres: Fly Managed Postgres (prints a connection string; or `fly mpg attach`), or a Neon
#    project. Use the *direct* connection string, not a transaction-mode pooler (no `-pooler` host).
fly mpg create --name "$APP-db" --region iad

# 4. Secrets (never in fly.toml). A Modal token lets the Machine create sandboxes:
#    modal.com > Settings > API Tokens > New token (or `modal token new` prints one).
fly secrets set --app "$APP" --stage \
  COVERSET_DATABASE_URL='postgresql://<user>:<password>@<host>:5432/<db>' \
  COVERSET_BLOB_URL="s3://$APP-state?endpoint_url=https://fly.storage.tigris.dev&region=auto" \
  COVERSET_PUBLIC_URL="https://$APP.fly.dev" \
  COVERSET_ADMIN_TOKEN="$(openssl rand -hex 32)" \
  COVERSET_SECRET_KEY="$(openssl rand -hex 32)" \
  MODAL_TOKEN_ID=ak-... \
  MODAL_TOKEN_SECRET=as-...

# 5. Build (remotely, on Fly's builder) and deploy. The first boot migrates the empty database
#    before it opens the port (the health check's grace period is 60 s for this).
fly deploy --config deploy/fly/fly.toml --dockerfile Dockerfile --app "$APP"

# 6. The URL, and a first look: every row should pass except the GitHub ones.
fly status --app "$APP"                       # Hostname: <app>.fly.dev
curl "https://$APP.fly.dev/healthz"           # {"status":"ok","version":"..."}
fly ssh console --app "$APP" -C "coverset deploy check"
```

`fly secrets set` without `--stage` restarts the Machine; with `--stage` the next `fly deploy` picks
the values up. You cannot read a secret back (`fly secrets list` shows digests only): save the admin
token in your password manager as you set it, or set a new one.

To check the bucket and database end to end before pointing GitHub at it, run the smoke script
from your laptop with the same two URLs and the Tigris keys (`fly ssh console -C env` shows them):
`uv run python scripts/smoke_hosted.py`, then `--clean-only` (docs/deploy.md, "Smoke-test a hosted
state").

## Then

* **GitHub App:** sign in at `https://<app>.fly.dev/login` with the admin token, open `/setup`, and
  follow [docs/github.md](../../docs/github.md#1-create-the-github-app-manifest-flow). The App is
  stored in the database, encrypted under `COVERSET_SECRET_KEY` (set above), so nothing needs
  copying into Fly secrets and no restart is needed; keep `COVERSET_SECRET_KEY` in your password
  manager (rotation: `coverset secrets rekey`, [docs/security.md](../../docs/security.md#5-secrets)).
* **Logs:** `fly logs --app "$APP"`.
* **Shell:** `fly ssh console --app "$APP"` (as root; the server runs as uid 10001).
* **Backups:** the database's own (Managed Postgres and Neon keep point-in-time history) and
  object versioning on the bucket; the Machine has nothing to back up.
* **Upgrade:** `git pull && fly deploy --config deploy/fly/fly.toml --dockerfile Dockerfile`.
  Migrations run before the new Machine listens. With one Machine expect a few seconds of 502s;
  `fly scale count 2` removes them (the leader lease keeps the dispatcher, reaper and gc in one).
* **Stop paying:** `fly scale count 0 --app "$APP"` (the database and bucket keep billing) or
  `fly apps destroy "$APP"`.

## Why these settings

| Setting | Why |
|---|---|
| no `[[mounts]]` | Nothing on the Machine must survive: Postgres and the bucket hold the state, `/data` and `/var/cache/coverset` are caches. Without a volume a Machine is not pinned to one host. |
| `auto_stop_machines = "off"`, `min_machines_running = 1` | Webhooks must not wait for a cold start, and the run dispatcher, reaper, gc and warm-pool maintainer live in the process. |
| `COVERSET_MIGRATE_ON_START = "true"`, `release_command` commented | Each Machine migrates before it listens; several starting together serialise on an advisory lock. Switch to the release command to migrate once per deploy. |
| `COVERSET_TRUST_PROXY=true` | Fly's proxy terminates TLS. coverset reads the client from `Fly-Client-IP` on Fly (Fly's `X-Forwarded-For` ends with the app's own IP) and the scheme from `X-Forwarded-Proto`. |
| `concurrency.type = "requests"`, `idle_timeout = 600` | SSE streams are long requests; coverset pings every 15 s. |
| `kill_timeout = 30` | `serve` drains requests for 10 s, then drains the Modal warm pool. |
| health check on `/healthz`, grace 60 s | Unauthenticated, cheap, and only 200 once the app serves (after the migration). |

## Files instead of a bucket on Fly

To keep the blobs as files instead of in Tigris (one Machine, still with Postgres), add a volume
back and leave `COVERSET_BLOB_URL` unset:

```toml
[[mounts]]
  source = "coverset_data"
  destination = "/data"
  initial_size = "10gb"
```

`fly volumes create coverset_data --region iad --size 10` first. The volume pins the Machine to one
host, Fly snapshots it daily (5 days), and there must only ever be one Machine.
