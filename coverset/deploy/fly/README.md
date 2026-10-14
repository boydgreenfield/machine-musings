# coverset on Fly.io

One always-on Machine (`shared-cpu-1x`, 1 GB) with a 10 GB volume at `/data`, TLS and a
`https://<app>.fly.dev` URL from Fly's proxy. About **$8.20/month** in `iad` (Machine $6.70, volume
$1.50, daily volume snapshots free up to 10 GB). Shards still run on Modal; the Machine only runs
the control plane. Not deployed from this repository yet: the commands below are checked against
Fly's documentation (October 2026), not run. See [docs/deploy.md](../../docs/deploy.md) for the
comparison with Modal and a VM.

Run everything from the **project root** (the Docker build context).

```sh
# 0. flyctl: https://fly.io/docs/flyctl/install/ ; then
fly auth login

# 1. Create the app from this config without deploying (pick a name; it rewrites `app` in the copy).
fly launch --no-deploy --copy-config --config deploy/fly/fly.toml --dockerfile Dockerfile --name coverset-<you>
APP=coverset-<you>

# 2. The volume (one Machine, one volume, same region as primary_region).
fly volumes create coverset_data --app "$APP" --region iad --size 10

# 3. Secrets (never in fly.toml). A Modal token lets the Machine create sandboxes:
#    modal.com > Settings > API Tokens > New token (or `modal token new` prints one).
fly secrets set --app "$APP" --stage \
  COVERSET_PUBLIC_URL="https://$APP.fly.dev" \
  COVERSET_ADMIN_TOKEN="$(openssl rand -hex 32)" \
  COVERSET_SECRET_KEY="$(openssl rand -hex 32)" \
  MODAL_TOKEN_ID=ak-... \
  MODAL_TOKEN_SECRET=as-...

# 4. Build (remotely, on Fly's builder) and deploy.
fly deploy --config deploy/fly/fly.toml --dockerfile Dockerfile --app "$APP"

# 5. The URL, and a first look.
fly status --app "$APP"                       # Hostname: <app>.fly.dev
curl "https://$APP.fly.dev/healthz"           # {"status":"ok","version":"..."}
fly ssh console --app "$APP" -C "coverset deploy check"
```

`fly secrets set` without `--stage` restarts the Machine; with `--stage` the next `fly deploy` picks
the values up. To read back the admin token you generated, you cannot (`fly secrets list` shows
digests only): save it in your password manager as you set it, or set a new one.

## Then

* **GitHub App:** sign in at `https://<app>.fly.dev/login` with the admin token, open `/setup`, and
  follow [docs/github.md](../../docs/github.md#1-create-the-github-app-manifest-flow). The App key and
  `.env` land on the volume (`/data`, the working directory), then `fly machine restart`.
* **Logs:** `fly logs --app "$APP"`.
* **Shell:** `fly ssh console --app "$APP"` (as root; the server runs as uid 10001).
* **Backups:** Fly snapshots the volume daily and keeps 5 days (`fly volumes snapshots list <vol id>`).
  SQLite's WAL makes a crash-consistent snapshot safe to restore. For an extra copy:
  `fly ssh console -C "python -c \"import sqlite3; sqlite3.connect('/data/coverset.db').backup(sqlite3.connect('/data/backup.db'))\""`
  then `fly sftp get /data/backup.db`.
* **Upgrade:** `git pull && fly deploy --config deploy/fly/fly.toml --dockerfile Dockerfile`. The
  Machine is replaced in place (a volume pins it to one host; expect ~20 s of 502s). Migrations run at
  startup.
* **Stop paying:** `fly scale count 0 --app "$APP"` (the volume keeps billing) or `fly apps destroy "$APP"`.

## Why these settings

| Setting | Why |
|---|---|
| `auto_stop_machines = "off"`, `min_machines_running = 1` | Webhooks must not wait for a cold start, and the run dispatcher, reaper, gc and warm-pool maintainer live in this process. |
| one Machine, `[[mounts]]` at `/data` | SQLite has one writer; a Fly volume is a local NVMe disk attached to one Machine, so WAL mode and file locks work normally. |
| `COVERSET_TRUST_PROXY=true` | Fly's proxy terminates TLS. coverset reads the client from `Fly-Client-IP` on Fly (Fly's `X-Forwarded-For` ends with the app's own IP) and the scheme from `X-Forwarded-Proto`. |
| `concurrency.type = "requests"`, `idle_timeout = 600` | SSE streams are long requests; coverset pings every 15 s. |
| `kill_timeout = 30` | `serve` drains requests for 10 s, then drains the Modal warm pool. |
| health check on `/healthz` | Unauthenticated, cheap, and only 200 once the app serves. |
