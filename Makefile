# Developer entry point. Run `make help` for targets.
# Windows: use Git Bash/WSL with make installed (e.g. `winget install ezwinports.make`).

COMPOSE      := docker compose --env-file docker/.env -f docker/compose.yml -f docker/compose.ports.yml
COMPOSE_DEV  := $(COMPOSE) -f docker/compose.dev.yml
INFRA        := postgres redis rabbitmq qdrant neo4j minio litellm
BACKEND      := cd apps/backend &&
FRONTEND     := cd apps/frontend &&
# uv workspace members, each tested/type-checked in its own run.
PY_MEMBERS   := libs/llm_client services/gateway services/api

.DEFAULT_GOAL := help
.PHONY: help env install up up-all down down-v logs ps models dev dev-backend dev-gateway \
        dev-frontend docker-dev migrate migration seed test test-unit lint format typecheck check

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

env: ## Create .env files from examples (won't overwrite)
	@test -f docker/.env || cp docker/.env.example docker/.env
	@test -f apps/backend/.env || cp apps/backend/.env.example apps/backend/.env
	@test -f apps/frontend/.env.local || cp apps/frontend/.env.example apps/frontend/.env.local
	@echo "env files ready"

install: ## Install backend + frontend deps and git hooks
	$(BACKEND) uv sync --all-packages
	$(FRONTEND) npm ci
	uvx pre-commit install

# ---------- Docker ----------
up: env ## Start infrastructure only (run apps locally with `make dev`)
	$(COMPOSE) up -d $(INFRA)

up-all: env ## Start the full stack in Docker (migrations run automatically)
	$(COMPOSE) up -d --build

docker-dev: env ## Full stack in Docker with hot reload
	$(COMPOSE_DEV) up --build

models: ## Start Ollama and pull the local models used by the gateway
	./scripts/pull-ollama-models.sh

down: ## Stop all containers
	$(COMPOSE) --profile llm-local down

down-v: ## Stop all containers and delete volumes
	$(COMPOSE) --profile llm-local down -v

logs: ## Tail logs (s=<service> to filter)
	$(COMPOSE) logs -f $(s)

ps: ## Show container status
	$(COMPOSE) ps

# ---------- Local dev ----------
dev: ## Run API, gateway and frontend locally with hot reload
	@$(MAKE) -j3 dev-backend dev-gateway dev-frontend

dev-backend:
	$(BACKEND) uv run uvicorn platform_api.main:app --reload --port 8000

# docker/.env supplies the provider API keys (deployments without a key are skipped).
dev-gateway:
	$(BACKEND) uv run uvicorn gateway.main:app --reload --port 8100 --env-file ../../docker/.env

dev-frontend:
	$(FRONTEND) npm run dev

# ---------- Database ----------
migrate: ## Apply DB migrations
	$(BACKEND) uv run alembic -c libs/db/alembic.ini upgrade head

migration: ## Create a migration: make migration m="add foo"
	$(BACKEND) uv run alembic -c libs/db/alembic.ini revision --autogenerate -m "$(m)"

seed: ## Create default tenant + admin user
	$(BACKEND) uv run python -m platform_api.cli.seed

# ---------- Quality ----------
test: ## Run all tests (API integration tests need `make up`)
	@for m in $(PY_MEMBERS); do echo "== $$m"; (cd apps/backend/$$m && uv run pytest) || exit 1; done
	$(FRONTEND) npm run lint

test-unit: ## Run tests that need no services
	@for m in libs/llm_client services/gateway; do (cd apps/backend/$$m && uv run pytest) || exit 1; done
	cd apps/backend/services/api && uv run pytest tests/unit

lint: ## Lint backend and frontend
	$(BACKEND) uv run ruff check . && uv run ruff format --check .
	$(FRONTEND) npm run lint

format: ## Auto-format backend
	$(BACKEND) uv run ruff check --fix . && uv run ruff format .

typecheck: ## Type-check backend and frontend
	$(BACKEND) uv run mypy libs/core/src libs/db/src
	@for m in $(PY_MEMBERS); do echo "== $$m"; (cd apps/backend/$$m && uv run mypy --config-file ../../pyproject.toml src tests) || exit 1; done
	$(FRONTEND) npx next typegen && npx tsc --noEmit

check: lint typecheck test ## Everything CI runs
