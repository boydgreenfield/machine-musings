# coverset developer tasks. `make help` lists them; docs/dev.md explains the loop.
# Every recipe prints what it runs. Override the variables on the command line, e.g.
#   make dev BIND=127.0.0.1:8123
#   make test WORKERS=4

UV ?= uv
BIND ?= 127.0.0.1:8000
WORKERS ?= 6
QUICK_MARKERS ?= not slow and not modal and not network
# The dev services' Postgres (deploy/dev-compose.yml publishes it on 55432, clear of a local Postgres on
# 5432; COVERSET_DEV_PG_PORT moves both). test-pg runs the suite against it.
DEV_PG_PORT ?= $(or $(COVERSET_DEV_PG_PORT),55432)
TEST_DATABASE_URL ?= postgresql+asyncpg://coverset:coverset@127.0.0.1:$(DEV_PG_PORT)/coverset
# coverset dev --hosted's data dir (commands/dev.py DEV_HOSTED_DATA_DIR). Its cache and the demo's git
# repository belong to the dev database, so services-reset deletes it with the volumes. Deliberately
# not COVERSET_DATA_DIR, which may name the laptop's SQLite data.
HOSTED_DEV_DATA_DIR ?= data/hosted-dev

.DEFAULT_GOAL := help
.PHONY: help dev dev-hosted services services-down services-reset services-psql test-pg serve demo-reset check test test-plugin-312 test-vitest shots readme lint format clean

help: ## List the targets
	@echo "coverset make targets (variables: BIND=$(BIND) WORKERS=$(WORKERS)):"
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-16s %s\n", $$1, $$2}'

dev: ## Seed the demo once, then serve with reload on BIND (coverset dev)
	@echo "==> coverset dev --bind $(BIND): seeds an empty data dir, restarts on .py changes"
	$(UV) run coverset dev --bind $(BIND)

# Postgres + MinIO from deploy/dev-compose.yml instead of SQLite + files (docs/dev.md, "Hosted-like
# development"); coverset dev --hosted starts the stack itself when it is down.
dev-hosted: ## coverset dev against Postgres + MinIO (starts deploy/dev-compose.yml if needed)
	@echo "==> coverset dev --hosted --bind $(BIND): COVERSET_DATABASE_URL/BLOB_URL and the MinIO keys point at the dev services"
	$(UV) run coverset dev --hosted --bind $(BIND)

services: ## Start the dev Postgres 17 + MinIO (deploy/dev-compose.yml) and wait until they are healthy
	@echo "==> docker compose -f deploy/dev-compose.yml up -d --wait: Postgres on 127.0.0.1:$(DEV_PG_PORT), MinIO on 127.0.0.1:9000 (console :9001)"
	docker compose -f deploy/dev-compose.yml up -d --wait

services-down: ## Stop the dev services, keeping their data (services-reset deletes it)
	@echo "==> docker compose -f deploy/dev-compose.yml down: the volumes stay"
	docker compose -f deploy/dev-compose.yml down

services-reset: ## Stop the dev services and delete their data (Postgres and MinIO volumes, HOSTED_DEV_DATA_DIR)
	@echo "==> docker compose -f deploy/dev-compose.yml down -v: drops the dev database and bucket; then $(HOSTED_DEV_DATA_DIR), whose cache and demo repository belong to them"
	docker compose -f deploy/dev-compose.yml down -v
	rm -rf $(HOSTED_DEV_DATA_DIR)

services-psql: ## psql inside the dev Postgres container
	@echo "==> psql -U coverset coverset in the dev Postgres container"
	docker compose -f deploy/dev-compose.yml exec postgres psql -U coverset coverset

serve: ## Serve with reload, no seeding (coverset serve --reload)
	@echo "==> coverset serve --reload --bind $(BIND)"
	$(UV) run coverset serve --reload --bind $(BIND)

demo-reset: ## Empty the data dir and rebuild the demo (coverset demo --fresh --no-serve)
	@echo "==> coverset demo --fresh --no-serve: wipes COVERSET_DATA_DIR (only a coverset data dir) and reseeds"
	$(UV) run coverset demo --fresh --no-serve

lint: ## ruff check
	@echo "==> ruff check"
	$(UV) run ruff check .

format: ## ruff format (rewrites files) and ruff check --fix
	@echo "==> ruff format, then ruff check --fix"
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

check: ## ruff check + format --check + the quick tests (no slow, modal or network)
	@echo "==> ruff check"
	$(UV) run ruff check .
	@echo "==> ruff format --check"
	$(UV) run ruff format --check .
	@echo "==> pytest -m '$(QUICK_MARKERS)' -n $(WORKERS)"
	$(UV) run pytest -q -n $(WORKERS) -p no:cacheprovider -m "$(QUICK_MARKERS)"

test: ## The whole suite in parallel (pytest -n WORKERS)
	@echo "==> pytest -n $(WORKERS): the whole suite"
	$(UV) run pytest -q -n $(WORKERS) -p no:cacheprovider

test-pg: services ## The whole suite against the dev Postgres (a fresh database per test); leaves the services running
	@echo "==> pytest -n $(WORKERS) with COVERSET_TEST_DATABASE_URL=$(TEST_DATABASE_URL)"
	COVERSET_TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(UV) run pytest -q -n $(WORKERS) -p no:cacheprovider

# --with-editable, not --with: uv caches a local directory's built wheel until its pyproject.toml
# changes, so --with would test a stale copy of the plugin's sources.
test-plugin-312: ## The pytest plugin's tests on Python 3.12, isolated from the project venv
	@echo "==> pytest-coverset tests on Python 3.12 (uv run --isolated --no-project, -n $(WORKERS))"
	$(UV) run --isolated --no-project --python 3.12 \
		--with-editable ./packages/pytest-coverset --with-editable ./packages/coverset-model \
		--with pytest==9.1.1 --with hypothesis==6.168.5 --with pytest-xdist \
		pytest -q -n $(WORKERS) packages/pytest-coverset/tests

test-vitest: ## The Vitest reporter's own tests (npm test in packages/coverset-vitest)
	@echo "==> npm test in packages/coverset-vitest (needs node and npm install there once)"
	cd packages/coverset-vitest && npm test

shots: ## Retake the README and docs screenshots (Playwright Chromium)
	@echo "==> scripts/screenshots.py: seeds a scratch demo and shoots every page"
	$(UV) run python scripts/screenshots.py

readme: ## Rewrite the README's measured numbers from docs/numbers.json
	@echo "==> scripts/check_readme.py --fix"
	$(UV) run python scripts/check_readme.py --fix

clean: ## Remove build and tool caches (never data/, .venv or node_modules)
	@echo "==> removing __pycache__, .pytest_cache, .ruff_cache, build/ and *.egg-info"
	find src tests scripts packages -type d -name node_modules -prune -o -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache build *.egg-info
	rm -rf packages/pytest-coverset/build packages/coverset-model/build packages/coverset-probe/build
	find packages -maxdepth 3 -type d -name '*.egg-info' -not -path '*/node_modules/*' -prune -exec rm -rf {} +
