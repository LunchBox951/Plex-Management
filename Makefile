.DEFAULT_GOAL := help
.PHONY: help install lint format type test check run migrate openapi docker-build \
	ui-install gen-client ui-check ui-build

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Editable install with dev extras + pre-commit hooks
	pip install -e ".[dev]"
	pre-commit install

lint: ## Lint with ruff
	ruff check .

format: ## Auto-format with ruff
	ruff format .

type: ## Type-check with pyright (strict)
	pyright

test: ## Run the test suite with coverage
	pytest

check: ## Run all CI gates locally
	python -m plex_manager.web.openapi_export
	git diff --exit-code docs/api/openapi.json
	ruff check .
	ruff format --check .
	pyright
	pytest
	$(MAKE) ui-check
	$(MAKE) ui-build

run: ## Run the app locally
	python -m plex_manager

migrate: ## Apply database migrations (creates ./data if needed)
	alembic upgrade head

openapi: ## Export the OpenAPI document to docs/api/openapi.json
	python -m plex_manager.web.openapi_export

docker-build: ## Build the container image locally
	docker build -t plex-manager:dev .

ui-install: ## Install frontend dependencies (npm ci)
	npm --prefix frontend ci

gen-client: ## Regenerate the typed API client from docs/api/openapi.json
	npm --prefix frontend run gen:client

ui-check: ## Frontend gates: client-drift, typecheck, lint, unit tests
	npm --prefix frontend run gen:check
	npm --prefix frontend run typecheck
	npm --prefix frontend run lint
	npm --prefix frontend run test

ui-build: ## Build the SPA into src/plex_manager/web/static
	npm --prefix frontend run build
