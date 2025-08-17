SHELL := /bin/bash

.PHONY: up down fmt lint test build docker
up:
	docker compose -f deploy/docker/docker-compose.yml up --build

down:
	docker compose -f deploy/docker/docker-compose.yml down -v

fmt:
	black backend || true
	npx prettier -w frontend/web || true

lint:
	rufflehog --version >/dev/null 2>&1 || true
	python -m ruff backend || true
	npx eslint frontend/web --ext .ts,.tsx || true

test:
	pytest -q || true

build:
	docker compose -f deploy/docker/docker-compose.yml build

docker:
	docker compose -f deploy/docker/docker-compose.yml ps
