.PHONY: help setup dev dev-local dev-docker dev-docker-up dev-docker-down dev-docker-logs dev-docker-shell dev-docker-seed prod-test-pull prod-test-up prod-test-refresh prod-test-down prod-test-logs prod-test-shell run lint format format-fix typecheck test test-unit test-slow test-integration test-api test-providers test-utilities test-a11y test-e2e test-e2e-chrome test-e2e-firefox coverage coverage-check migrate migration seed seed-full reset-db reset-password reset-import import-fixture performance-baseline direct-download-baseline validate runner-preflight release-changelog-check workflow-hygiene secret-scan security-ci ci-local docker-build-check docker-smoke ci-full ci-clean-room security-check pre-commit css-build css-watch clean

VENV := .venv
PYTHON_BOOTSTRAP ?= python3
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
SECRET := PULLBOX_SECRET_KEY=dev-secret-key-for-local-development-00000001
DB_FILE := data/pullbox.db
ALEMBIC := $(VENV)/bin/alembic -c alembic/alembic.ini
PULLBOX_IMAGE ?= ghcr.io/pullboxapp/pullbox:latest
DEV_DOCKER_PORT ?= 8585
DEV_DOCKER_COMPOSE := PULLBOX_DEV_PORT=$(DEV_DOCKER_PORT) docker compose -f docker/docker-compose.dev.yml
DEV_DOCKER_URL ?= http://127.0.0.1:$(DEV_DOCKER_PORT)
PERFORMANCE_BASELINE_URL ?= $(DEV_DOCKER_URL)
PERFORMANCE_BASELINE_ARGS ?=
TOOLS_DIR := .cache/tools
ACTIONLINT := $(TOOLS_DIR)/actionlint
GITLEAKS := $(TOOLS_DIR)/gitleaks

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Setup ───────────────────────────────────────────────

setup: ## Create venv, install Python + Node dependencies
	$(PYTHON_BOOTSTRAP) -m venv $(VENV)
	$(PIP) install --upgrade "pip>=26.0" wheel
	$(PIP) install -e ".[dev,e2e]"
	$(VENV)/bin/pre-commit install
	npm install
	@if [ ! -f .env ]; then \
		cp .env.dev.example .env; \
		echo "\033[33mℹ️  Created .env from .env.dev.example\033[0m"; \
	fi
	@echo ""
	@echo "\033[32m✅ Setup complete.\033[0m Run \033[36mmake dev-local\033[0m or \033[36mmake dev-docker\033[0m to start developing."

dev: dev-local ## Friendly default contributor workflow
dev-local: setup migrate seed run ## First-time local venv workflow: setup + migrate + seed + run server

# ─── Run ─────────────────────────────────────────────────

run: ## Start the local dev server with runtime-parity config resolution
	$(SECRET) $(PYTHON) -m pullbox.devserver --reload

# ─── Local Docker Dev ────────────────────────────────────

dev-docker: dev-docker-up dev-docker-seed ## One-command Docker dev: build, start, wait, and seed starter data
	@echo ""
	@echo "\033[32m✅ Docker dev is ready at $(DEV_DOCKER_URL).\033[0m"
	@echo "\033[36mUse 'make dev-docker-logs' to follow logs and 'make dev-docker-down' to stop it.\033[0m"

dev-docker-up: ## Build and start local Docker dev in the background
	@mkdir -p media-docker-dev/comics media-docker-dev/downloads media-docker-dev/imports
	$(DEV_DOCKER_COMPOSE) up --build -d
	@echo "\033[36mWaiting for Docker dev server at $(DEV_DOCKER_URL)...\033[0m"
	@for i in $$(seq 1 60); do \
		if curl -sf "$(DEV_DOCKER_URL)/ping" >/dev/null 2>&1; then \
			echo "\033[32m  ✅ Docker dev server is healthy after $${i}s\033[0m"; \
			exit 0; \
		fi; \
		sleep 1; \
	done; \
	echo "\033[31m  ❌ Docker dev server did not become healthy.\033[0m"; \
	$(DEV_DOCKER_COMPOSE) logs --tail=120 pullbox; \
	exit 1

dev-docker-down: ## Stop the local Docker dev stack
	$(DEV_DOCKER_COMPOSE) down

dev-docker-logs: ## Tail the local Docker dev logs
	$(DEV_DOCKER_COMPOSE) logs -f pullbox

dev-docker-shell: ## Open a Python REPL in the running local Docker dev container
	$(DEV_DOCKER_COMPOSE) exec pullbox python

dev-docker-seed: ## Seed the local Docker dev database with starter data
	$(DEV_DOCKER_COMPOSE) exec -T pullbox python scripts/seed_dev_data.py

# ─── Local Production-Image Test ─────────────────────────

prod-test-pull: ## Pull the production-style image for local verification (override PULLBOX_IMAGE=...)
	PULLBOX_IMAGE=$(PULLBOX_IMAGE) docker compose -f docker/docker-compose.prod-test.yml pull

prod-test-up: ## Start the local production-image test stack on http://127.0.0.1:18585
	PULLBOX_IMAGE=$(PULLBOX_IMAGE) docker compose -f docker/docker-compose.prod-test.yml up -d

prod-test-refresh: ## Pull the latest configured image and restart the local production-image test stack
	PULLBOX_IMAGE=$(PULLBOX_IMAGE) docker compose -f docker/docker-compose.prod-test.yml pull
	PULLBOX_IMAGE=$(PULLBOX_IMAGE) docker compose -f docker/docker-compose.prod-test.yml up -d

prod-test-down: ## Stop the local production-image test stack
	PULLBOX_IMAGE=$(PULLBOX_IMAGE) docker compose -f docker/docker-compose.prod-test.yml down

prod-test-logs: ## Tail logs for the local production-image test stack
	PULLBOX_IMAGE=$(PULLBOX_IMAGE) docker compose -f docker/docker-compose.prod-test.yml logs -f pullbox

prod-test-shell: ## Open a Python REPL in the shell-less local production-image test container
	PULLBOX_IMAGE=$(PULLBOX_IMAGE) docker compose -f docker/docker-compose.prod-test.yml exec pullbox python

# ─── CSS Build (Tailwind) ───────────────────────────────

css-build: ## Compile Tailwind CSS (minified)
	npm run css:build

css-watch: ## Watch and recompile Tailwind CSS on changes
	BROWSERSLIST_IGNORE_OLD_DATA=1 npx @tailwindcss/cli -i src/pullbox/ui/static/css/input.css -o src/pullbox/ui/static/css/tailwind.css --watch

# ─── Lint & Format ───────────────────────────────────────

lint: ## Run ruff linter
	$(VENV)/bin/ruff check src/ tests/

format: ## Run ruff formatter (check only)
	$(VENV)/bin/ruff format --check src/ tests/

format-fix: ## Auto-format with ruff
	$(VENV)/bin/ruff format src/ tests/

typecheck: ## Run mypy strict
	$(VENV)/bin/mypy --strict src/pullbox/

# ─── Tests ───────────────────────────────────────────────

test: ## Run all tests (parallel)
	$(SECRET) $(VENV)/bin/pytest tests/ --ignore=tests/e2e -n auto -v

test-unit: ## Run fast unit tests only (~15-20s)
	$(SECRET) $(VENV)/bin/pytest tests/unit/ -v

test-slow: ## Run slow tests only (parallel)
	$(SECRET) $(VENV)/bin/pytest tests/ -v -n auto -m "slow"

test-integration: ## Run integration tests (parallel)
	$(SECRET) $(VENV)/bin/pytest tests/integration/ -n auto -v

test-api: ## Run API tests (parallel)
	$(SECRET) $(VENV)/bin/pytest tests/api/ -n auto -v

test-providers: ## Run provider tests (parallel)
	$(SECRET) $(VENV)/bin/pytest tests/providers/ -n auto -v

test-utilities: ## Run utility tests (parallel)
	$(SECRET) $(VENV)/bin/pytest tests/utilities/ -n auto -v

test-e2e: ## Run E2E browser tests across all browsers in isolated pytest sessions (requires Playwright)
	$(MAKE) test-e2e-chrome
	$(MAKE) test-e2e-firefox

test-a11y: ## Run accessibility checks (contrast audit + Playwright WCAG scans in Chromium)
	$(SECRET) $(PYTHON) scripts/check_ui_contrast.py
	$(SECRET) $(VENV)/bin/pytest tests/e2e/ -v -m accessibility --browser chromium

test-e2e-chrome: ## Run E2E browser tests in Chromium only
	$(SECRET) $(VENV)/bin/pytest tests/e2e/ -v -m "not accessibility" --browser chromium

test-e2e-firefox: ## Run E2E browser tests in Firefox only
	$(SECRET) $(VENV)/bin/pytest tests/e2e/ -v -m "not accessibility" --browser firefox

coverage: ## Run tests with coverage report (parallel)
	$(SECRET) $(VENV)/bin/pytest tests/ --ignore=tests/e2e -n auto --cov=pullbox --cov-report=term-missing

coverage-check: ## Run tests with 90% coverage threshold (parallel)
	$(SECRET) $(VENV)/bin/pytest tests/ --ignore=tests/e2e -n auto --cov=pullbox --cov-report=term-missing --cov-fail-under=90

# ─── Database ────────────────────────────────────────────

migrate: ## Run database migrations
	@mkdir -p data/backups data/comics data/comics/.covers data/logs data/tmp
	$(SECRET) $(ALEMBIC) upgrade head

migration: ## Create a new migration (usage: make migration m="description")
	$(SECRET) $(ALEMBIC) revision --autogenerate -m "$(m)"

seed: ## Seed minimal development data (admin user, 5 series, 16 issues)
	$(SECRET) $(PYTHON) scripts/seed_dev_data.py

seed-full: ## Seed comprehensive data for all features (downloads, blocklist, search logs, etc.)
	$(SECRET) $(PYTHON) scripts/seed_full_dev_data.py

reset-db: ## Delete DB, re-migrate, and re-seed from scratch
	@echo "\033[33m⚠️  Deleting database and re-creating from scratch...\033[0m"
	rm -f $(DB_FILE) $(DB_FILE)-shm $(DB_FILE)-wal
	@mkdir -p data/backups data/comics data/comics/.covers data/logs data/tmp
	$(SECRET) $(ALEMBIC) upgrade head
	$(SECRET) $(PYTHON) scripts/seed_full_dev_data.py
	@echo ""
	@echo "\033[32m✅ Database reset complete.\033[0m"

# ─── Validation ──────────────────────────────────────────

validate: css-build lint format typecheck test ## Run full validation (css + lint + format + typecheck + tests)

runner-preflight: ## Verify local runner prerequisites for CI lanes
	@PATH="$(CURDIR)/$(VENV)/bin:$$PATH" .github/scripts/preflight-runner.sh base python node playwright

release-changelog-check: ## Verify CHANGELOG.md has a curated release section (usage: make release-changelog-check VERSION=X.Y.Z)
	@test -n "$(VERSION)" || (echo "VERSION is required, e.g. make release-changelog-check VERSION=1.0.0" >&2; exit 1)
	$(PYTHON_BOOTSTRAP) scripts/extract_changelog_section.py --version "$(VERSION)" --check

workflow-hygiene: ## Run local workflow linting with pinned actionlint
	@echo "\033[36m──── Workflow Hygiene ────\033[0m"
	@bash scripts/install_ci_tool.sh actionlint "$(TOOLS_DIR)"
	$(ACTIONLINT) -shellcheck= -pyflakes=

secret-scan: ## Run blocking local gitleaks scan with the repo baseline
	@echo "\033[36m──── Secret Scan ────\033[0m"
	@bash scripts/install_ci_tool.sh gitleaks "$(TOOLS_DIR)"
	$(GITLEAKS) dir . --no-banner --redact --timeout=300

security-ci: ## Run the local security lane (pip-audit blocking; safety/bandit advisory)
	@echo "\033[36m──── Security Checks ────\033[0m"
	@VENV_BIN="$(VENV)/bin" bash scripts/security_check.sh

ci-local: ## Run exactly what CI runs (lint + format + typecheck + migrate-check + tailwind-check + all tests incl E2E)
	@echo "\033[36m──── Runner Preflight ────\033[0m"
	@$(MAKE) runner-preflight
	@echo "\033[36m──── Node Dependencies ────\033[0m"
	npm ci
	@echo "\033[36m──── CSS Build ────\033[0m"
	npm run css:build
	@echo "\033[36m──── Tailwind Drift Check ────\033[0m"
	@git diff --exit-code src/pullbox/ui/static/css/tailwind.css || \
		(echo "\033[31m❌ Tailwind CSS output is stale. Run 'make css-build' and commit.\033[0m" && exit 1)
	@echo "\033[32m  ✅ Tailwind CSS is up to date\033[0m"
	@echo "\033[36m──── Lint ────\033[0m"
	$(VENV)/bin/ruff check src/ tests/
	@echo "\033[36m──── Format ────\033[0m"
	$(VENV)/bin/ruff format --check src/ tests/
	@echo "\033[36m──── Type Check ────\033[0m"
	$(VENV)/bin/mypy --strict src/pullbox/
	@echo "\033[36m──── Migration Check ────\033[0m"
	$(SECRET) PULLBOX_DB_URL="sqlite+aiosqlite:///test_migration_check.db" $(ALEMBIC) upgrade head
	$(SECRET) PULLBOX_DB_URL="sqlite+aiosqlite:///test_migration_check.db" $(ALEMBIC) downgrade base
	rm -f test_migration_check.db
	@echo "\033[36m──── App Boot Check ────\033[0m"
	$(SECRET) PULLBOX_DB_URL="sqlite+aiosqlite:///test_boot.db" $(ALEMBIC) upgrade head
	$(SECRET) PULLBOX_DB_URL="sqlite+aiosqlite:///test_boot.db" $(PYTHON) scripts/run_with_timeout.py 10 $(PYTHON) -c "from pullbox.app import create_app; create_app(); print('App factory created successfully')"
	rm -f test_boot.db
	@echo "\033[36m──── Tests (unit + integration + API + providers + utilities + UI) ────\033[0m"
	$(SECRET) $(VENV)/bin/pytest tests/ --ignore=tests/e2e -n auto --cov=pullbox --cov-report=term-missing --cov-fail-under=90 -v
	@echo "\033[36m──── Accessibility Checks (contrast + Playwright WCAG) ────\033[0m"
	$(SECRET) $(PYTHON) scripts/check_ui_contrast.py
	$(SECRET) $(VENV)/bin/pytest tests/e2e/ -v -m accessibility --browser chromium
	@echo "\033[36m──── E2E Tests (Playwright - Chromium) ────\033[0m"
	$(SECRET) $(VENV)/bin/pytest tests/e2e/ -v -m "not accessibility" --browser chromium
	@echo "\033[36m──── E2E Tests (Playwright - Firefox) ────\033[0m"
	$(SECRET) $(VENV)/bin/pytest tests/e2e/ -v -m "not accessibility" --browser firefox
	@echo ""
	@echo "\033[32m✅ All CI checks passed (including E2E).\033[0m"

# NOTE: Schema drift check (alembic check) is skipped because SQLite produces false
# positives for Enum→VARCHAR type comparisons and FK constraints. Enable when
# CI migrates to PostgreSQL.

# ─── Docker ──────────────────────────────────────────────

docker-build-check: ## Build the production Docker image and verify it can be inspected
	@echo "\033[36m──── Docker Preflight ────\033[0m"
	@.github/scripts/preflight-runner.sh base docker
	@echo "\033[36m──── Docker Build ────\033[0m"
	@BUILD_DATE="$$(date -u +%Y-%m-%dT%H:%M:%SZ)"; \
	GIT_SHA="$$(git rev-parse HEAD)"; \
	GIT_BRANCH="$$(git rev-parse --abbrev-ref HEAD)"; \
	VERSION="$$(PYTHONPATH=src $(PYTHON) -c 'from pullbox import __version__; print(__version__)' 2>/dev/null || echo 0.0.0-dev)"; \
	docker build -t pullbox:local -f docker/Dockerfile \
		--build-arg BUILD_DATE="$$BUILD_DATE" \
		--build-arg GIT_SHA="$$GIT_SHA" \
		--build-arg GIT_BRANCH="$$GIT_BRANCH" \
		--build-arg VERSION="$$VERSION" \
		.
	@echo "\033[36m──── Docker Inspect ────\033[0m"
	@docker image inspect pullbox:local >/dev/null

docker-smoke: docker-build-check ## Run Docker smoke tests against the locally built production image
	@echo "\033[36m──── Start Container ────\033[0m"
	@docker rm -f pullbox-smoke 2>/dev/null || true
	docker run -d --name pullbox-smoke -p 18585:8585 \
		-e PULLBOX_SECRET_KEY=smoke-test-secret-key-for-local-ci \
		-e PULLBOX_LOG_LEVEL=WARNING \
		pullbox:local
	@echo "\033[36m──── Health Check ────\033[0m"
	@for i in $$(seq 1 30); do \
		if curl -sf http://localhost:18585/ping > /dev/null 2>&1; then \
			echo "\033[32m  ✅ Container healthy after $${i}s\033[0m"; \
			break; \
		fi; \
		if [ $$i -eq 30 ]; then \
			echo "\033[31m  ❌ Container failed to become healthy\033[0m"; \
			docker logs pullbox-smoke; \
			docker rm -f pullbox-smoke; \
			exit 1; \
		fi; \
		sleep 1; \
	done
	@echo "\033[36m──── Smoke Tests ────\033[0m"
	PULLBOX_SMOKE_URL=http://localhost:18585 $(SECRET) $(VENV)/bin/pytest tests/e2e/test_smoke.py -v || \
		(docker logs pullbox-smoke; docker rm -f pullbox-smoke; exit 1)
	@echo "\033[36m──── Teardown ────\033[0m"
	@docker rm -f pullbox-smoke
	@echo ""
	@echo "\033[32m✅ Docker smoke tests passed.\033[0m"

ci-full: ## Full local gate: GitHub-aligned CI + security + Docker smoke
	@$(MAKE) workflow-hygiene
	@$(MAKE) secret-scan
	@$(MAKE) security-ci
	@$(MAKE) ci-local
	@$(MAKE) docker-smoke
	@echo ""
	@echo "\033[32m✅ Full CI pipeline passed (including Docker).\033[0m"

ci-clean-room: ## Run the clean-room fresh-install verification lane locally
	@echo "\033[36m──── Clean-Room Verification ────\033[0m"
	@bash scripts/run_clean_room.sh

pre-commit: ## Run pre-commit hooks on all files
	$(VENV)/bin/pre-commit run --all-files

# ─── Admin Utilities ─────────────────────────────────────

reset-password: ## Reset a local dev password (usage: make reset-password u="admin" p="NewPass1!")
	$(SECRET) $(PYTHON) scripts/reset_password.py "$(u)" "$(p)"

reset-import: ## Undo a test import (restores files, cleans DB). Add DRY_RUN=1 to preview.
	$(SECRET) $(PYTHON) scripts/reset_test_import.py

import-fixture: ## Extract test import fixture to data/test-import/ for manual import testing
	$(PYTHON) scripts/generate_import_fixture.py

mylar-import-benchmark: ## Benchmark trusted Mylar migration without external metadata calls
	$(PYTHON) scripts/benchmark_mylar3_import.py $(MYLAR_IMPORT_BENCHMARK_ARGS)

performance-baseline: ## Capture a JSON performance baseline for the active dev server
	@mkdir -p data/performance
	$(SECRET) $(PYTHON) scripts/performance_baseline.py \
		--base-url "$(PERFORMANCE_BASELINE_URL)" \
		$(PERFORMANCE_BASELINE_ARGS) \
		--output data/performance/baseline.json

direct-download-baseline: ## Capture the offline DD-0 workload baseline
	@mkdir -p data/performance
	$(PYTHON) scripts/benchmark_direct_download_readiness.py \
		$(DIRECT_DOWNLOAD_BASELINE_ARGS) \
		--output data/performance/direct-download-readiness.json

security-check: ## Run the local GitHub-style security pipeline
	@$(MAKE) secret-scan
	@$(MAKE) security-ci

# ─── Cleanup ─────────────────────────────────────────────

clean: ## Remove build artifacts and caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -f .coverage
	rm -f test_migration_check.db
	rm -rf node_modules/
