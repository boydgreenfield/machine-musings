# syntax=docker/dockerfile:1.7
# coverset control plane: web UI, webhooks, API, run dispatcher (docs/deploy.md).
#
#   docker build -t coverset .
#   docker run -p 8000:8000 -v coverset-data:/data -e COVERSET_DATABASE_URL=postgresql://... \
#     -e COVERSET_ADMIN_TOKEN=$(openssl rand -hex 32) -e COVERSET_PUBLIC_URL=http://localhost:8000 coverset
#
# The rows are in Postgres (COVERSET_DATABASE_URL). The blobs are files under /data (a volume), or,
# with COVERSET_BLOB_URL (an S3-compatible bucket) plus the bucket's AWS_* credentials (or an
# instance/task role), in the bucket, and the container is stateless: /data and the cache
# (/var/cache/coverset, on the container's own disk) are disposable, so no volume is needed.
#
# At start `serve` runs `coverset init-db` first (COVERSET_MIGRATE_ON_START=true, the default): it
# creates the database when the server has none of its name, then migrates. Every replica may do
# so: an advisory lock (coverset:migrate) runs them one after the other.
# To migrate once per deploy instead, set it false and run `init-db` in a release step
# (docs/deploy.md, "Migrations on deploy"). The GitHub App that /setup creates is stored in the
# database, encrypted under COVERSET_SECRET_KEY: nothing on the container's disk holds a credential.
#
# The project is installed *editable* on purpose: the executors find the pytest-coverset sources and
# coverset's own uv.lock relative to src/coverset (env_export.PLUGIN_DIR), and the demo seeds from
# examples/ and scripts/. uv stays in the final image because the executors call it at run time
# (`uv build` of the plugin wheel, `uv export` of a target's lock, the local executor's venvs).

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.10.7

# ---- build: resolve and install the locked dependencies into /app/.venv -------------------------
FROM python:${PYTHON_VERSION}-slim AS build
ARG UV_VERSION
# uv from PyPI (a static binary) rather than ghcr.io/astral-sh/uv: one registry fewer to reach.
RUN pip install --no-cache-dir --root-user-action=ignore "uv==${UV_VERSION}"
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first (cached until uv.lock or a pyproject changes), then the project itself.
COPY pyproject.toml uv.lock README.md ./
COPY packages/coverset-model/pyproject.toml packages/coverset-model/README.md packages/coverset-model/
COPY packages/pytest-coverset/pyproject.toml packages/pytest-coverset/README.md packages/pytest-coverset/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-install-workspace

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime ---------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 coverset \
    && useradd --system --uid 10001 --gid coverset --home-dir /home/coverset --create-home coverset \
    && mkdir -p /data /var/cache/coverset \
    && chown coverset:coverset /data /var/cache/coverset && chmod 700 /data /var/cache/coverset
COPY --from=build /usr/local/bin/uv /usr/local/bin/uv
COPY --from=build --chown=root:root /app /app

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    COVERSET_ENV=prod \
    COVERSET_BIND=0.0.0.0:8000 \
    COVERSET_DATA_DIR=/data \
    COVERSET_CACHE_DIR=/var/cache/coverset \
    COVERSET_MIGRATE_ON_START=true
USER coverset
# The working directory is the data directory (an optional `.env` there is read like anywhere else).
WORKDIR /data
# Pre-build the pytest-coverset wheel into ~/.cache/coverset so the first run does not wait for it.
RUN python -c "from coverset.executors.env_export import build_plugin_wheel; print(build_plugin_wheel())"

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
# tini reaps git's and uv's children and forwards SIGTERM, so `serve` drains and stops the pool;
# arguments go to `coverset` (so `docker run coverset init-db` runs `coverset init-db`).
ENTRYPOINT ["tini", "--", "coverset"]
CMD ["serve", "--bind", "0.0.0.0:8000"]
