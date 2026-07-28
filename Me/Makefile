SHELL := /bin/bash
COMPOSE := docker compose -f deploy/docker/docker-compose.yml

.PHONY: help up down logs fmt lint audit verify test test-web test-e2e test-all build docker clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Start the full local stack
	$(COMPOSE) up --build

down: ## Stop the stack and remove volumes
	$(COMPOSE) down -v

logs: ## Tail service logs
	$(COMPOSE) logs -f

fmt: ## Format Python and web sources
	python -m black backend
	cd frontend/web && npx prettier -w src

# NOTE: these targets no longer end in "|| true" — a lint/test failure must
# actually fail the build instead of silently reporting success in CI.
lint: ## Lint Python and web sources
	python -m ruff check backend
	cd frontend/web && npx tsc --noEmit

audit: ## Fail on known vulnerabilities in shipped dependencies
	bash scripts/audit_python.sh
	bash scripts/audit_node.sh

test: ## Run the backend test suite (unit + e2e)
	python -m pytest backend/tests -q

test-web: ## Run frontend unit + integration tests
	cd frontend/web && npm test

test-e2e: ## Run browser end-to-end tests (needs: npx playwright install)
	cd frontend/web && npm run test:e2e

test-all: test test-web ## Run every suite that needs no browser download

# Everything CI enforces, in one command. Use this before claiming green:
# "it passed on my machine" and "the gates pass" are different statements,
# and this is the one that can be reproduced by someone else.
verify: ## Run every CI gate locally (lint, audits, tests, typecheck, build)
	@set -e; \
	echo "== 1/8 ruff ==";              python -m ruff check backend; \
	echo "== 2/8 pip-audit ==";         bash scripts/audit_python.sh; \
	echo "== 3/8 pytest ==";            python -m pytest backend/tests -q; \
	echo "== 4/8 npm audit ==";         bash scripts/audit_node.sh; \
	echo "== 5/8 tsc (app) ==";         cd frontend/web && npx tsc --noEmit && cd ../..; \
	echo "== 6/8 tsc (e2e specs) ==";   cd frontend/web && npx tsc --noEmit -p tsconfig.e2e.json && cd ../..; \
	echo "== 7/8 vitest ==";            cd frontend/web && npx vitest run && cd ../..; \
	echo "== 8/8 web build ==";         cd frontend/web && npm run build && cd ../..; \
	echo; echo "All 8 gates passed."
	@echo "NOTE: browser E2E is not included — it needs 'npx playwright install chromium'."
	@echo "      Run 'make test-e2e' separately where a browser can be downloaded."

build: ## Build all container images
	$(COMPOSE) build

docker: ## Show container status
	$(COMPOSE) ps

clean: ## Remove caches and build output
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache frontend/web/dist
