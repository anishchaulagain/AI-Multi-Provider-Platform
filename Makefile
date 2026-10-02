# Developer entry point. Run `make help` for targets.
# Windows: use Git Bash/WSL with make installed (e.g. `winget install ezwinports.make`).

COMPOSE      := docker compose --env-file docker/.env -f docker/compose.yml -f docker/compose.ports.yml
COMPOSE_DEV  := $(COMPOSE) -f docker/compose.dev.yml
INFRA        := postgres redis rabbitmq qdrant neo4j minio litellm
BACKEND      := cd apps/backend &&
FRONTEND     := cd apps/frontend &&

.DEFAULT_GOAL := help
.PHONY: help env install up up-all down down-v logs ps dev dev-backend dev-frontend \
        docker-dev migrate migration seed test test-unit lint format typecheck check

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

env: ## Create .env files from examples (won't overwrite)
	@test -f docker/.env || cp docker/.env.example docker/.env
	@test -f apps/backend/.env || cp apps/backend/.env.example apps/backend/.env
	@test -f apps/frontend/.env.local || cp apps/frontend/.env.example apps/frontend/.env.local
	@echo "env files ready"

install: ## Install backend + frontend deps and git hooks
	$(BACKEND) uv sync
	$(FRONTEND) npm ci
	uvx pre-commit install

# ---------- Docker ----------
up: env ## Start infrastructure only (run apps locally with `make dev`)
	$(COMPOSE) up -d $(INFRA)

up-all: env ## Start the full stack in Docker
	$(COMPOSE) up -d --build

docker-dev: env ## Full stack in Docker with hot reload
	$(COMPOSE_DEV) up --build

down: ## Stop all containers
	$(COMPOSE) down

down-v: ## Stop all containers and delete volumes
	$(COMPOSE) down -v

logs: ## Tail logs (s=<service> to filter)
	$(COMPOSE) logs -f $(s)

ps: ## Show container status
	$(COMPOSE) ps

# ---------- Local dev ----------
dev: ## Run backend and frontend locally with hot reload
	@$(MAKE) -j2 dev-backend dev-frontend

dev-backend:
	$(BACKEND) uv run uvicorn app.main:app --reload --port 8000

dev-frontend:
	$(FRONTEND) npm run dev

# ---------- Database ----------
migrate: ## Apply DB migrations
	$(BACKEND) uv run alembic upgrade head

migration: ## Create a migration: make migration m="add foo"
	$(BACKEND) uv run alembic revision --autogenerate -m "$(m)"

seed: ## Create default tenant + admin user
	$(BACKEND) uv run python -m scripts.seed

# ---------- Quality ----------
test: ## Run all tests (integration tests need `make up`)
	$(BACKEND) uv run pytest
	$(FRONTEND) npm run lint

test-unit: ## Run backend unit tests only
	$(BACKEND) uv run pytest tests/unit

lint: ## Lint backend and frontend
	$(BACKEND) uv run ruff check . && uv run ruff format --check .
	$(FRONTEND) npm run lint

format: ## Auto-format backend
	$(BACKEND) uv run ruff check --fix . && uv run ruff format .

typecheck: ## Type-check backend and frontend
	$(BACKEND) uv run mypy app scripts tests
	$(FRONTEND) npx next typegen && npx tsc --noEmit

check: lint typecheck test ## Everything CI runs
