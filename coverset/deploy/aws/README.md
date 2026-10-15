# coverset on AWS

Two shapes, both with shards on Modal:

* **ECS Fargate + RDS + S3** (stateless, the production shape): the image from ECR, Postgres on
  RDS, facts in an S3 bucket the task reaches through its role. The recipe is in
  [docs/deploy.md](../../docs/deploy.md#aws-ecs-fargate-rds-s3); this directory holds its
  [task definition](ecs-task-definition.json) and the [least-privilege bucket policy](iam-policy.json)
  for the task role. Config only: not deployed (no AWS account was used).
* **One VM (Lightsail or EC2)**, below: the cheapest always-on option.

## One VM

One Ubuntu 24.04 machine runs [`deploy/compose.yml`](../compose.yml): coverset, Postgres 17 and
Caddy, which gets a Let's Encrypt certificate on its own. With `COVERSET_STATE=hosted` the script
also runs the compose file's `hosted` profile: MinIO on the same VM, its data on the data disk, so
coverset's own container is disposable; the default (`files`) keeps the blobs as files on the data
disk. Shards still run on Modal; the VM only runs the control plane. Not deployed from this repository: [`user-data.sh`](user-data.sh) passes `bash -n` and
shellcheck and was dry-run in an `ubuntu:24.04` container with Docker stubbed out; the compose stack
itself was run locally with Caddy's internal TLS (docs/deploy.md has what was verified).

**Cheapest:** Lightsail, `$12/month` for 2 GB / 2 vCPU / 60 GB SSD (the `$7` 1 GB bundle works, but
the image build is tight), plus `$0.10/GB-month` for an optional block-storage disk. With
`COVERSET_STATE=hosted` take the `$24` 4 GB bundle: Postgres and MinIO share the machine. EC2
`t4g.small` (arm64, 2 GB) is about `$12/month` on demand plus a gp3 volume (`$0.08/GB-month`).

### Lightsail (console, five minutes)

1. **Create instance** → Linux/Unix → OS Only → **Ubuntu 24.04 LTS**, bundle `$12`.
2. **Launch script**: paste [`user-data.sh`](user-data.sh) with the variables at the top filled in:
   `REPO_URL` (an https URL the instance can clone; for a private repository use a URL with a
   read-only fine-grained token, or leave it empty and copy the source to `/opt/coverset/src`
   before running the script by hand), `REPO_REF`, `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`,
   optionally `COVERSET_DOMAIN`, and `COVERSET_STATE=hosted` for Postgres + MinIO.
3. **Networking**: attach a **static IP**, then in the IPv4 firewall allow `HTTPS (443)` and
   `HTTP (80)` (Let's Encrypt's HTTP challenge and the redirect); restrict `SSH (22)` to your address.
4. Optional **block storage** disk (attach before first boot or rerun the script): it is formatted
   and mounted at `/srv/coverset/data` automatically.
5. Wait about 5 minutes, then `ssh ubuntu@<ip> sudo cat /root/coverset-admin-token` and open
   `https://<ip-with-dashes>.sslip.io/login` (or your domain).

Without `COVERSET_DOMAIN` the script uses `<public-ip-with-dashes>.sslip.io`, a public wildcard DNS
name that resolves to the IP, so Caddy gets a real certificate without a domain. For your own
domain, point an `A` record at the static IP first, then set `COVERSET_DOMAIN`.

### EC2 (same script)

* AMI: Ubuntu Server 24.04 LTS (amd64 or arm64), `t4g.small` or `t3.small`.
* Storage: root 20 GB gp3, plus a second gp3 volume (10-50 GB) for `/srv/coverset/data`; the script
  formats and mounts the one empty non-root disk (`DATA_DEVICE=auto`) and makes Docker wait for it.
* User data: [`user-data.sh`](user-data.sh) with the variables filled in.
* An Elastic IP so the name (and the GitHub webhook URL) survives a stop/start.
* IMDSv2 is fine (the script asks for a token).
* Backups: a Data Lifecycle Manager policy for daily snapshots of the data volume; the script's
  `/etc/cron.daily/coverset-backup` also writes a `pg_dump` of the database to
  `/srv/coverset/data/backups` every night (kept 7 days), so a snapshot always holds a clean copy.

#### Security group (inbound)

| Port | Source | Why |
|---|---|---|
| 443/tcp | 0.0.0.0/0, ::/0 | HTTPS: the UI, the API and GitHub's webhooks |
| 443/udp | 0.0.0.0/0, ::/0 | HTTP/3 (optional) |
| 80/tcp | 0.0.0.0/0, ::/0 | Let's Encrypt HTTP-01 challenge and the redirect to HTTPS |
| 22/tcp | your address only | SSH (or none, with SSM Session Manager) |

Outbound: allow all (GitHub's API, Modal's API, PyPI and the package indexes the plugin wheel build
uses, Let's Encrypt). coverset itself listens only inside the compose network; port 8000 is never
published.

### Operating it

```sh
cd /opt/coverset/src/coverset/deploy
sudo docker compose --env-file /etc/coverset.compose.env ps
sudo docker compose --env-file /etc/coverset.compose.env logs -f coverset
sudo docker compose --env-file /etc/coverset.compose.env exec coverset coverset deploy check
# upgrade: rerun the script (it fetches REPO_REF, rebuilds, restarts; ~20 s of 502s)
sudo REPO_URL=... bash /var/lib/cloud/instance/scripts/part-001   # EC2 user data; or your copy
```

Settings live in `/opt/coverset/coverset.env` (root only); edit, then
`docker compose --env-file /etc/coverset.compose.env up -d`. The GitHub App flow at `/setup` stores
the App in Postgres, encrypted under `COVERSET_SECRET_KEY` (which `user-data.sh` generates into
`coverset.env`): keep that key, since the nightly `pg_dump` alone cannot open the App.

The Postgres (and MinIO) passwords are in `/opt/coverset/services.env` (root only) and the matching
URLs in `/opt/coverset/coverset.env`; `/srv/coverset/data` holds `app/` (coverset's data dir: the
blobs without `COVERSET_STATE=hosted`, and the cache), `postgres/`, `minio/` and
`backups/` (a nightly `pg_dump`, 7 days). psql: `sudo docker compose --env-file /etc/coverset.compose.env exec postgres
psql -U coverset coverset`. MinIO publishes no port; list the bucket from inside its container
with `mc alias set vm http://localhost:9000 <MINIO_ROOT_USER> <MINIO_ROOT_PASSWORD>` and then
`mc ls --recursive vm/coverset` (`docker compose ... exec minio sh`).
