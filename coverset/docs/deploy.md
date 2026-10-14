# Deploying coverset

coverset's control plane (the web UI, `/webhooks/github`, the `/v1` API, the run dispatcher, the
reaper and the warm-pool maintainer) is one Python process with one SQLite database and a data
directory of Parquet facts and git mirrors. Shards run elsewhere: in Modal Sandboxes
(`COVERSET_EXECUTOR=modal`) whichever host serves the control plane. A deployment therefore needs
one always-on process, a disk that survives restarts, HTTPS on a public name GitHub can reach, and
Modal credentials.

Three ways are ready, one of them live:

| | **Modal** (`deploy/modal/`) | **Fly.io** (`deploy/fly/`) | **VM: Lightsail / EC2** (`deploy/aws/`, `deploy/compose.yml`) |
|---|---|---|---|
| Status | **deployed** 2026-10-13, tested end to end | config + commands, not deployed (no Fly account tonight) | compose stack run locally with Caddy; user data dry-run in Ubuntu 24.04 |
| URL | `https://<workspace>-coverset--coverset-web.<region>.modal.run` | `https://<app>.fly.dev` | `https://<ip-with-dashes>.sslip.io` or your domain |
| Cost (control plane, idle) | about **$0.76/day** (~$23/month; 0.5 core + 1 GiB reserved, billed as max(reserved, used)); Starter plan's $30/month credit covers it | about **$8.20/month** (shared-cpu-1x 1 GB $6.70 + 10 GB volume $1.50) | Lightsail **$12/month** (2 GB); EC2 t4g.small ~$12/month + gp3 |
| Persistence | SQLite on container-local disk, backed up to the Volume `coverset-data` every 60 s when changed; facts and mirrors on the Volume | Fly volume (local NVMe), SQLite WAL as on a laptop | EBS / block-storage disk mounted at `/srv/coverset/data` |
| Recovery point | 60 s on a crash; zero on a stop or a `--strategy recreate` deploy | last write (the volume is the disk); daily snapshots kept 5 days | last write; nightly SQLite copy + EBS/Lightsail snapshots |
| SSE (run pages, `coverset-ci wait`) | works; streams of 210-330 s measured with silent gaps of 110 s (the documented 150 s limit is for non-streaming responses) | works (`idle_timeout = 600`, pings every 15 s) | works (Caddy `flush_interval -1`) |
| Cold start | none in normal operation (`min_containers=1`); a restart is ~10-15 s | none (`auto_stop_machines = "off"`); deploy ~20 s of 502s | none; upgrade ~20 s |
| How the Modal executor authenticates | the container runtime (no token stored) | `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` Fly secrets | `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` in `/opt/coverset/coverset.env` |
| `COVERSET_TRUST_PROXY` | `false`: Modal puts the real client and `https` in the ASGI scope itself and strips client-sent `X-Forwarded-*` (measured) | `true` (client from `Fly-Client-IP`, scheme from `X-Forwarded-Proto`) | `true` (Caddy sets both headers) |
| Upgrades | `modal deploy --strategy recreate ...` (deploy 30-55 s, then 15-25 s without service) | `fly deploy` | rerun `user-data.sh` or `docker compose up -d --build` |
| Logs | `modal app logs coverset-web -e coverset [-f]` | `fly logs` | `docker compose logs -f coverset` |
| Shell | `modal container exec <id> -- sh` | `fly ssh console` | `ssh` + `docker compose exec coverset sh` |

**Pick Modal** to have a URL tonight with nothing else to run: one command, the executor needs no
token, and the cost sits inside the Starter credit. **Pick a VM or Fly** when the database should
be a plain disk with point-in-time durability, or once several people depend on it. The image
(`Dockerfile`) is the same everywhere except Modal, which builds the same contents natively.

## The live Modal deployment

* **URL:** `https://boydgreenfield-coverset--coverset-web.us-east.modal.run`
* **Admin token:** `~/.config/coverset/modal-web-admin-token` on the machine it was deployed from
  (mode 600). Sign in at `/login`, or `curl -H "Authorization: Bearer $(cat ~/.config/coverset/modal-web-admin-token)" <url>/`.
* **Stop it** (stops billing; the Volume and Secret stay): `uv run modal app stop coverset-web -e coverset`
* **Start / upgrade it:** `uv run modal deploy --strategy recreate -e coverset deploy/modal/coverset_web.py`

### What is in Modal

| Object | Name (environment `coverset`) | Holds |
|---|---|---|
| App | `coverset-web` | one function, `web`: `@modal.asgi_app`, `min_containers=1`, `max_containers=1`, `@modal.concurrent(max_inputs=256)`, 24 h per-request timeout |
| Volume | `coverset-data` (v1), mounted at `/data` | `COVERSET_DATA_DIR`: facts, mirrors, runs, `instance_id`; `backup/` (database generations); the working directory, so `/setup`'s `.env` and `secrets/github-app.pem` persist |
| Secret | `coverset-web` | `COVERSET_ADMIN_TOKEN`, `COVERSET_SECRET_KEY`, `COVERSET_PUBLIC_URL`, `COVERSET_TRUST_PROXY=false`, `COVERSET_EXECUTOR=modal`, `COVERSET_ENV=prod`; add GitHub keys here or let `/setup` write them |
| App | `coverset-runner` (existing) | the shard sandboxes the web container creates |

The image is `debian_slim` (Python 3.13) + git + uv 0.10.7 + `uv sync --frozen --no-dev` of the
project (editable, as the Dockerfile does), with `.dockerignore` reused as the upload filter; the
plugin wheel is pre-built into the image. Deploy-time knobs: `COVERSET_WEB_CPU` (default 0.5 cores)
and `COVERSET_WEB_MEMORY_MIB` (1024).

### Storage: why the database is not on the Volume

Modal Volumes have explicit `commit()`/`reload()`, last-writer-wins semantics and, per the docs,
no distributed file locking; the docs' only SQLite example builds the file on local disk and copies
it to the Volume. Measured inside the container: `flock`, `lockf`, `O_EXCL` and `rename` work on the
mounted Volume, **hard links fail with EPERM**, and WAL-mode SQLite runs there but 200 one-row
commits took **235 ms vs 9 ms** on local disk. What a background commit captures of a database in
mid-transaction is undocumented. So:

1. The live database is `/root/coverset-data/coverset.db` on the container's disk
   (`COVERSET_DATABASE_URL` is set by the module).
2. At startup it is restored from `/data/backup/coverset.db` (or `coverset.db.prev` if the newest
   fails `PRAGMA quick_check`; a bad file is renamed `*.corrupt-<time>`, never rotated over).
3. Every 60 s, only if `PRAGMA data_version` moved, SQLite's online backup API copies it to a
   self-contained file, which replaces `backup/coverset.db` (the old one becomes `.prev`) followed by
   `volume.commit()`. One dated copy per UTC day is kept in `backup/daily/` (7 days). A 356 KB
   database backs up in 0.7-1.0 s.
4. The ASGI lifespan shutdown (container stop, redeploy) takes a final backup.

**Recovery point:** 60 s for a crash, OOM kill or host loss; zero for a stop or a recreate deploy.
Runs in flight when the container stops are marked abandoned at the next start (set
`COVERSET_REQUEUE_ON_BOOT=true` in the Secret to re-queue them). GitHub redelivers webhooks that got
no answer.

The module pre-creates `/data/instance_id` with a rename, because the executor's `os.link`-based
creation fails on the Volume (see open issue in the executor).

### Redeploys must use `--strategy recreate`

With the default rolling strategy the new version waits for a container slot, and with
`max_containers=1` the healthy old container never frees it: on 2026-10-13 the URL answered nothing
for 25 minutes ("waiting to be scheduled on a CPU worker") until the old container was stopped by
hand (`modal container stop -y <id>`), after which the new one served in 14 s. `recreate` stops the
old container first, which is also what makes the recovery point zero.

### Measured on 2026-10-13 (modal 1.6.1, us-east)

| What | Result |
|---|---|
| `GET /healthz` over the public URL, from a laptop on the US east coast | 0.15-0.17 s |
| dashboard with a bearer token | 0.34 s |
| `/login` (form, cookie `Secure`, redirect to `https://`), `/setup` with the token | 200 |
| `POST /webhooks/github` with no secret configured | 503 `webhook_secret_unset` (fails closed) |
| ASGI lifespan | Quart's `before_serving` runs at container start (migrations, dispatcher, gc, reaper, pool maintainer, backup timer); `after_serving` runs on stop ("warm pool drained", final backup) |
| a real run queued inside the container (`coverset run --repo <demo app> --head main --executor modal --no-wait`) | the dispatcher built the env image (11.2 s, first time), ran 97 tests in Modal sandboxes, **PASS**; its `/v1/runs/<id>/events` SSE stream delivered 97 `test` events and `done` in 26.6 s |
| SSE through Modal's gateway | a 210 s stream with an event every 15 s and a 330 s stream with 110 s of silence between events both completed with HTTP 200; coverset's own run-page stream (replay at 0.07x) stayed open 216 s and ended with `done` |
| request headers seen by the app | scope client = the caller's address, scheme `https`, `Via: 2.0 Caddy`; a client-sent `X-Forwarded-For: 6.6.6.6` was not passed through |
| redeploy (code change) | image layers after the dependency sync rebuild in ~10 s; deploy 31-54 s; a `--strategy recreate` deploy answered `/healthz` again 23 s after the deploy command returned |
| admin token rotation (new Secret, then a recreate deploy) | old token 401, new token 200, the earlier run still there |
| container replaced | the earlier run was still there afterwards (restored from the Volume) |
| where it runs | the container reported `MODAL_REGION=eu-west` while the URL's gateway is `us-east`; no region is pinned (`region=` on the function would pin it; Modal prices region selection separately) |

## Docker image

`Dockerfile` (multi-stage, `python:3.13-slim`, uv 0.10.7 from PyPI, `uv sync --frozen --no-dev`,
git, tini, non-root uid 10001, `HEALTHCHECK` on `/healthz`, `COVERSET_DATA_DIR=/data` as a volume and
working directory, `ENTRYPOINT ["tini", "--", "coverset"]`, `CMD ["serve", "--bind", "0.0.0.0:8000"]`).
1.09 GB; a cold build takes about 2 min 45 s, a source-only rebuild seconds. The project is
installed editable on purpose (the executors find `packages/pytest-coverset` and `uv.lock` relative
to `src/coverset`), and `uv` stays in the image (plugin wheel builds, `uv export` of targets).

```sh
docker build -t coverset .
docker run -d --name coverset -p 8000:8000 -v coverset-data:/data \
  -e COVERSET_ADMIN_TOKEN="$(openssl rand -hex 32)" -e COVERSET_PUBLIC_URL=http://localhost:8000 coverset
curl localhost:8000/healthz
docker exec coverset coverset deploy check --offline
```

Verified locally: `/healthz`, `/login`, a bearer page, the Docker health status `healthy`,
`coverset replay fixtures/webhooks/pr_opened.json` answered 202 (with `COVERSET_ENV=test` and the
fixtures' secret; a second replay is `duplicate`), and after `docker restart` the repository and the
delivery were still there. `docker compose` with Caddy (`tls internal`): HTTPS 200, HSTS,
`http://` redirected to `https://`, the login redirect and `Secure` cookie on `https://`, and 22
failed logins with a different spoofed `X-Forwarded-For` each were still rate limited after 20
(coverset keys on the address Caddy saw).

## Proxies, cookies and HSTS

* `COVERSET_TRUST_PROXY=true` installs `coverset.web.proxy.ProxyHeadersMiddleware`: the client
  address comes from the **last** `X-Forwarded-For` value (the one the nearest proxy appended;
  `Fly-Client-IP` on a Fly Machine) and the scheme from the last `X-Forwarded-Proto`. Values that
  are not IP addresses or `http`/`https` are ignored; `X-Forwarded-Host` is never honoured. Only
  set it when every request comes through a proxy that sets those headers. The headers stay visible,
  so the `/setup` "local operator" path stays closed behind a proxy (sign in instead).
* `COVERSET_SECRET_KEY` (32+ characters) makes the cookie key independent of the data directory.
  The admin token is still mixed in, so rotating either signs everyone out.
* With an `https://` `COVERSET_PUBLIC_URL` every response carries
  `Strict-Transport-Security: max-age=31536000` and cookies are `Secure`.

## Rotating secrets

| Secret | Modal | Fly | VM |
|---|---|---|---|
| admin token (`COVERSET_ADMIN_TOKEN`) | recreate the Secret with all its keys (`modal secret create coverset-web --force --from-dotenv <file> -e coverset`), then a recreate deploy (verified) or `modal container stop -y <id>` (the replacement container starts in ~15 s and reads the Secret then) | `fly secrets set COVERSET_ADMIN_TOKEN=...` (restarts) | edit `/opt/coverset/coverset.env`, `docker compose up -d` |
| cookie key (`COVERSET_SECRET_KEY`) | same | same | same |
| Modal token | none stored | `fly secrets set MODAL_TOKEN_ID=... MODAL_TOKEN_SECRET=...`, revoke the old one in Modal | edit the env file |
| GitHub App key / webhook secret | regenerate in GitHub's App settings, then `/setup` with `overwrite=1`, or set the values in the Secret / env | same | same |

`--force` replaces the whole Secret, so keep the file (or your password manager) as the source of
truth. Every rotation of the token or key signs every browser out.

## Setting up the GitHub App on the public URL

1. `coverset deploy check` inside the deployment should show only the GitHub rows as warnings.
   Modal: `uv run modal container list -e coverset`, then
   `uv run modal container exec --no-pty <id> -- sh -c 'cd /data && coverset deploy check'` (11 PASS,
   2 WARN on 2026-10-13); Fly: `fly ssh console -C "coverset deploy check"`; VM:
   `docker compose exec coverset coverset deploy check`.
2. Sign in at `<url>/login` with the admin token. The `/setup` callback accepts an admin session
   behind a proxy (the "local operator" path needs a loopback connection without proxy headers).
3. Open `<url>/setup` (or `/setup?org=<org>`), press **Create the GitHub App**, confirm on GitHub.
   The manifest uses `COVERSET_PUBLIC_URL` for the webhook, the setup redirect and the sign-in
   callback (`/auth/github/callback`), so the public URL must be final before this step.
4. coverset writes the private key to `secrets/github-app.pem` and the App's ids and secrets to
   `.env` in its working directory, which is the data volume on every host here.
5. Restart so the settings are read again: Modal `modal container stop -y <id>`; Fly
   `fly machine restart`; VM `docker compose restart coverset`.
6. Install the App on the repositories (the success page links to it). Pushes and pull requests
   now arrive at `/webhooks/github`; runs go to Modal.

[github.md](github.md) has the details of the flow, token mode and the Actions step.

## `coverset deploy check`

Prints one `PASS`/`WARN`/`FAIL` row per readiness item and exits 1 on any `FAIL`: environment,
bind, public URL (https?), a `GET <public url>/healthz` probe (`--offline` skips it), admin token
(length only), secret-key source, proxy trust (aware of Modal), data dir writable and free disk,
database revision against the packaged head (never creates the file), executor and where the Modal
client's credentials come from, GitHub App key and client credentials, webhook secret. `--json`
prints one object per row.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Modal URL times out after a deploy, logs say "waiting to be scheduled on a CPU worker" | a rolling deploy deadlocked on `max_containers=1`: `modal container list -e coverset`, then `modal container stop -y <old id>`; deploy with `--strategy recreate` |
| Modal: `ASGI lifespan startup failed` with `PermissionError ... os.link` | the Volume refuses hard links; the module pre-creates `instance_id` (keep that call before `create_app`) |
| Modal: every link is `http://` / login loops | `COVERSET_PUBLIC_URL` does not match the URL Modal printed (it contains the region, e.g. `.us-east.modal.run`) |
| `curl` to the container dies with exit 132, or Python dies with "Illegal instruction" on import of `cryptography` | Docker Desktop on Apple M4: OpenSSL's ARM feature probe hits SIGILL in the VM. Run with `-e OPENSSL_armcap=0` locally; not needed on x86 or Graviton servers |
| `docker build`/`pull` hangs at "docker-credential-desktop get" | the macOS keychain prompt cannot be answered from a script: run from a terminal, or with `DOCKER_CONFIG` pointing at a config without `credsStore` |
| webhooks answer 503 `webhook_secret_unset` | no GitHub webhook secret yet: finish `/setup` (or set `COVERSET_GITHUB_WEBHOOK_SECRET`) and restart |
| webhooks answer 403 | signature mismatch: the App's webhook secret and coverset's differ; redeliver from the App's Advanced tab after fixing |
| GitHub says the webhook URL is invalid | it must be the public `https://` URL, not loopback; check `COVERSET_PUBLIC_URL` |
| `/setup/github/callback` answers 403 "Not allowed" | not signed in: sign in with the admin token first, then restart the flow at `/setup` |
| everyone shares one rate limit / login lockout | the proxy's address is the client: set `COVERSET_TRUST_PROXY=true` behind Caddy/Fly/an ALB (not on Modal) |
| `coverset deploy check` FAIL executor | no Modal credentials: `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` (Fly, VM) |
| a run stays queued | `COVERSET_PAUSED=true`, the daily budget is spent (`COVERSET_MODAL_DAILY_BUDGET_USD`), or `COVERSET_MAX_CONCURRENT_RUNS` are busy; see the run page |
| data seems to have rolled back on Modal | a crash between backups loses up to 60 s; `modal volume ls coverset-data /backup -e coverset` shows the generations and `daily/` copies |
