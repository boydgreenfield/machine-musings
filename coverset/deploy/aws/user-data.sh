#!/usr/bin/env bash
# coverset on one Ubuntu 24.04 VM (EC2 or Lightsail): Docker, then deploy/compose.yml (coverset +
# Postgres + Caddy for HTTPS), with the data on an attached EBS / block-storage disk when there is one.
# Paste as EC2 user data or a Lightsail launch script, or run it as root on a fresh instance. It is
# idempotent: running it again pulls the latest source and restarts the stack. docs: deploy/aws/README.md
#
# Fill these in (or export them before running by hand). Everything else is generated.
set -euo pipefail

COVERSET_DOMAIN="${COVERSET_DOMAIN:-}"            # empty: <public-ip-with-dashes>.sslip.io (real Let's Encrypt cert, no DNS needed)
COVERSET_CADDY_TLS="${COVERSET_CADDY_TLS:-}"      # "tls internal" for a self-signed certificate instead of Let's Encrypt
REPO_URL="${REPO_URL:-}"                          # git URL of the repository holding coverset (https, or empty if /opt/coverset/src was copied in)
REPO_REF="${REPO_REF:-main}"                      # branch, tag or commit
REPO_SUBDIR="${REPO_SUBDIR:-coverset}"            # where the coverset project sits inside that repository ("." for its root)
MODAL_TOKEN_ID="${MODAL_TOKEN_ID:-}"              # modal.com > Settings > API Tokens (shards run in Modal Sandboxes)
MODAL_TOKEN_SECRET="${MODAL_TOKEN_SECRET:-}"
DATA_DEVICE="${DATA_DEVICE:-auto}"                # block device for /srv/coverset/data; "auto" = the one empty non-root disk, "none" = root disk
COVERSET_STATE="${COVERSET_STATE:-files}"         # "files": blobs as files in the data dir; "hosted": blobs in MinIO on this VM (compose profile). Postgres runs either way

DATA_DIR=/srv/coverset/data
SRC=/opt/coverset/src
ENV_FILE=/opt/coverset/coverset.env
SERVICES_ENV_FILE=/opt/coverset/services.env      # the Postgres (and, hosted, MinIO) passwords
APP_UID=10001                                     # the image's `coverset` user
export DEBIAN_FRONTEND=noninteractive

log() { echo "[coverset user-data] $*"; }

# --- Docker Engine + compose plugin (Docker's apt repository) ---------------------------------------
if ! command -v docker >/dev/null; then
  log "installing Docker"
  apt-get update -q
  apt-get install -yq ca-certificates curl git
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

# --- Data disk ---------------------------------------------------------------------------------------
mkdir -p "$DATA_DIR"
if [ "$DATA_DEVICE" = "auto" ]; then
  root_disk=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" 2>/dev/null || true)
  DATA_DEVICE=none
  for disk in $(lsblk -dpno NAME,TYPE | awk '$2 == "disk" {print $1}'); do
    [ "$(basename "$disk")" = "$root_disk" ] && continue
    [ -n "$(lsblk -no MOUNTPOINT "$disk" | tr -d '[:space:]')" ] && continue
    [ "$(lsblk -no NAME "$disk" | wc -l)" -gt 1 ] && continue   # partitioned: not ours
    DATA_DEVICE=$disk
    break
  done
fi
if [ "$DATA_DEVICE" != "none" ]; then
  if ! blkid "$DATA_DEVICE" >/dev/null 2>&1; then
    log "formatting $DATA_DEVICE (ext4)"
    mkfs.ext4 -q -L coverset-data "$DATA_DEVICE"
  fi
  uuid=$(blkid -s UUID -o value "$DATA_DEVICE")
  if ! grep -q "$uuid" /etc/fstab; then
    echo "UUID=$uuid $DATA_DIR ext4 defaults,nofail,noatime 0 2" >> /etc/fstab
  fi
  mountpoint -q "$DATA_DIR" || mount "$DATA_DIR"
  # Docker must not start the stack before the disk is mounted (it would write to the root disk).
  mkdir -p /etc/systemd/system/docker.service.d
  printf '[Unit]\nRequiresMountsFor=%s\n' "$DATA_DIR" > /etc/systemd/system/docker.service.d/coverset-data.conf
  systemctl daemon-reload
  log "data on $DATA_DEVICE at $DATA_DIR"
else
  log "no separate data disk: data on the root disk at $DATA_DIR"
fi
chown "$APP_UID:$APP_UID" "$DATA_DIR"
chmod 700 "$DATA_DIR"

# --- Source ------------------------------------------------------------------------------------------
if [ -n "$REPO_URL" ]; then
  if [ -d "$SRC/.git" ]; then
    git -C "$SRC" fetch -q origin "$REPO_REF" && git -C "$SRC" checkout -q FETCH_HEAD
  else
    git clone -q "$REPO_URL" "$SRC" && git -C "$SRC" checkout -q "$REPO_REF"
  fi
fi
PROJECT="$SRC/$REPO_SUBDIR"
[ -f "$PROJECT/deploy/compose.yml" ] || { log "no coverset source at $PROJECT (set REPO_URL or copy it there)"; exit 1; }

# --- Public name ---------------------------------------------------------------------------------------
if [ -z "$COVERSET_DOMAIN" ]; then
  imds_token=$(curl -fsS -m 3 -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)
  public_ip=$(curl -fsS -m 3 -H "X-aws-ec2-metadata-token: $imds_token" http://169.254.169.254/latest/meta-data/public-ipv4 || true)
  [ -n "$public_ip" ] || public_ip=$(curl -fsS -m 5 https://checkip.amazonaws.com | tr -d '[:space:]')
  COVERSET_DOMAIN="${public_ip//./-}.sslip.io"
fi
log "public URL: https://$COVERSET_DOMAIN"

# --- Settings (generated once; edit $ENV_FILE and rerun to change) -------------------------------------
umask 077
if [ ! -f "$ENV_FILE" ]; then
  admin_token=$(openssl rand -hex 32)
  cat > "$ENV_FILE" <<EOF
COVERSET_ENV=prod
COVERSET_PUBLIC_URL=https://$COVERSET_DOMAIN
COVERSET_ADMIN_TOKEN=$admin_token
COVERSET_SECRET_KEY=$(openssl rand -hex 32)
COVERSET_EXECUTOR=modal
COVERSET_MODAL_ENVIRONMENT=coverset
MODAL_TOKEN_ID=$MODAL_TOKEN_ID
MODAL_TOKEN_SECRET=$MODAL_TOKEN_SECRET
EOF
  echo "$admin_token" > /root/coverset-admin-token
  log "admin token written to /root/coverset-admin-token (root only)"
fi
if [ ! -f "$SERVICES_ENV_FILE" ]; then
  cat > "$SERVICES_ENV_FILE" <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 24)
MINIO_ROOT_USER=coverset
MINIO_ROOT_PASSWORD=$(openssl rand -hex 24)
EOF
  log "Postgres and MinIO passwords written to $SERVICES_ENV_FILE (root only)"
fi
# shellcheck disable=SC1090
. "$SERVICES_ENV_FILE"
if ! grep -q '^COVERSET_DATABASE_URL=' "$ENV_FILE"; then
  echo "COVERSET_DATABASE_URL=postgresql+asyncpg://coverset:$POSTGRES_PASSWORD@postgres:5432/coverset" >> "$ENV_FILE"
fi
if [ "$COVERSET_STATE" = "hosted" ] && ! grep -q '^COVERSET_BLOB_URL=' "$ENV_FILE"; then
  # Blobs in MinIO beside coverset: coverset's own /data is then only a cache.
  cat >> "$ENV_FILE" <<EOF
COVERSET_BLOB_URL=s3://coverset?endpoint_url=http://minio:9000&region=us-east-1
AWS_ACCESS_KEY_ID=$MINIO_ROOT_USER
AWS_SECRET_ACCESS_KEY=$MINIO_ROOT_PASSWORD
EOF
fi
APP_DATA="$DATA_DIR/app"
mkdir -p "$APP_DATA" "$DATA_DIR/postgres" "$DATA_DIR/minio" "$DATA_DIR/backups"
chown "$APP_UID:$APP_UID" "$APP_DATA"
chmod 700 "$APP_DATA" "$DATA_DIR/backups"
if [ "$COVERSET_STATE" = "hosted" ]; then PROFILES=hosted; else PROFILES=; fi
umask 022

# --- Run ----------------------------------------------------------------------------------------------
cd "$PROJECT/deploy"
cat > /etc/coverset.compose.env <<EOF
COMPOSE_PROFILES=$PROFILES
COVERSET_ENV_FILE=$ENV_FILE
COVERSET_SERVICES_ENV_FILE=$SERVICES_ENV_FILE
COVERSET_DATA_PATH=$APP_DATA
COVERSET_PG_DATA_PATH=$DATA_DIR/postgres
COVERSET_MINIO_DATA_PATH=$DATA_DIR/minio
COVERSET_DOMAIN=$COVERSET_DOMAIN
COVERSET_CADDY_TLS=$COVERSET_CADDY_TLS
EOF
docker compose --env-file /etc/coverset.compose.env up -d --build --remove-orphans

# Nightly consistent copy of the database on the data disk (EBS snapshots then capture it too).
cat > /etc/cron.daily/coverset-backup <<EOF
#!/bin/sh
cd $PROJECT/deploy && docker compose --env-file /etc/coverset.compose.env exec -T postgres \\
  pg_dump -U coverset -d coverset --format=custom > $DATA_DIR/backups/coverset-\$(date +%F).dump
find $DATA_DIR/backups -name 'coverset-*.dump' -mtime +7 -delete
EOF
chmod 755 /etc/cron.daily/coverset-backup

for _ in $(seq 1 60); do
  curl -fsS -m 5 "https://$COVERSET_DOMAIN/healthz" >/dev/null 2>&1 && { log "up: https://$COVERSET_DOMAIN/healthz"; exit 0; }
  sleep 5
done
log "not answering on https://$COVERSET_DOMAIN yet: check 'docker compose logs' in $PROJECT/deploy"
