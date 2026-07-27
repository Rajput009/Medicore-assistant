SHELL := /bin/bash
COMPOSE := docker compose -f deploy/docker/docker-compose.yml

.PHONY: help up down logs fmt lint test build docker clean

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

test: ## Run the backend test suite
	python -m pytest backend/tests -q

build: ## Build all container images
	$(COMPOSE) build

docker: ## Show container status
	$(COMPOSE) ps

clean: ## Remove caches and build output
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache frontend/web/dist
