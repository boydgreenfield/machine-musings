# coverset developer tasks. `make help` lists them; docs/dev.md explains the loop.
# Every recipe prints what it runs. Override the variables on the command line, e.g.
#   make dev BIND=127.0.0.1:8123
#   make test WORKERS=4

UV ?= uv
BIND ?= 127.0.0.1:8000
WORKERS ?= 6
QUICK_MARKERS ?= not slow and not modal and not network

.DEFAULT_GOAL := help
.PHONY: help dev serve demo-reset check test test-plugin-312 test-vitest shots readme lint format clean

help: ## List the targets
	@echo "coverset make targets (variables: BIND=$(BIND) WORKERS=$(WORKERS)):"
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-16s %s\n", $$1, $$2}'

dev: ## Seed the demo once, then serve with reload on BIND (coverset dev)
	@echo "==> coverset dev --bind $(BIND): seeds an empty data dir, restarts on .py changes"
	$(UV) run coverset dev --bind $(BIND)

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
