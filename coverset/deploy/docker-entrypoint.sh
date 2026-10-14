#!/bin/sh
# coverset's container entrypoint (Dockerfile; tini runs it as PID 1's child).
#
# A hosted container is stateless: nothing on its disk survives a restart, so the GitHub App's
# private key cannot live where /setup writes it. Keep the PEM text in the platform's secrets as
# COVERSET_GITHUB_APP_PRIVATE_KEY (Fly secret, ECS/App Runner secret, Railway/Render variable) and
# it is written to COVERSET_GITHUB_APP_PRIVATE_KEY_PATH (mode 600) here, before coverset reads it.
#
# Then `coverset "$@"`: by default `serve`, which migrates the database first unless
# COVERSET_MIGRATE_ON_START=false (docs/deploy.md, "Migrations on deploy").
set -eu

if [ -n "${COVERSET_GITHUB_APP_PRIVATE_KEY:-}" ]; then
  key="${COVERSET_GITHUB_APP_PRIVATE_KEY_PATH:-secrets/github-app.pem}"
  mkdir -p "$(dirname "$key")"
  (umask 077 && printf '%s\n' "$COVERSET_GITHUB_APP_PRIVATE_KEY" > "$key")
fi

exec coverset "$@"
