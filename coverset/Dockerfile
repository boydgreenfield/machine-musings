# syntax=docker/dockerfile:1.7
# coverset control plane: web UI, webhooks, API, run dispatcher (docs/deploy.md).
#
#   docker build -t coverset .
#   docker run -p 8000:8000 -v coverset-data:/data \
#     -e COVERSET_ADMIN_TOKEN=$(openssl rand -hex 32) -e COVERSET_PUBLIC_URL=http://localhost:8000 coverset
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
    && mkdir -p /data && chown coverset:coverset /data && chmod 700 /data
COPY --from=build /usr/local/bin/uv /usr/local/bin/uv
COPY --from=build --chown=root:root /app /app

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    COVERSET_ENV=prod \
    COVERSET_BIND=0.0.0.0:8000 \
    COVERSET_DATA_DIR=/data
USER coverset
# The working directory holds the optional `.env` and `secrets/` (GitHub App key) that /setup
# writes, so it lives on the data volume too; see docs/deploy.md.
WORKDIR /data
# Pre-build the pytest-coverset wheel into ~/.cache/coverset so the first run does not wait for it.
RUN python -c "from coverset.executors.env_export import build_plugin_wheel; print(build_plugin_wheel())"

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
# tini reaps git's and uv's children and forwards SIGTERM, so `serve` drains and stops the pool.
ENTRYPOINT ["tini", "--", "coverset"]
CMD ["serve", "--bind", "0.0.0.0:8000"]
