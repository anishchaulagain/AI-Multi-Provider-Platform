# Backend

FastAPI Platform API for the AI Multi-Provider Platform.

## Setup

```bash
# from repo root: start Postgres, Redis, RabbitMQ, etc.
docker compose --env-file docker/.env -f docker/compose.yml -f docker/compose.ports.yml up -d postgres redis rabbitmq

cd apps/backend
cp .env.example .env
uv sync
uv run alembic upgrade head      # create tables
uv run python -m scripts.seed    # default tenant + admin@example.com / admin12345
```

With `make` installed, run `make up && make migrate && make seed` from the repo root instead.

## Run

```bash
uv run uvicorn app.main:app --reload --port 8000
```

| Endpoint | Description |
|---|---|
| `GET /healthz` | Liveness check (never touches dependencies) |
| `GET /readyz` | Readiness: database, Redis and RabbitMQ (returns 503 if any is down) |
| `POST /api/v1/auth/login` | Email + password → JWT (rate limited per IP) |
| `GET /api/v1/auth/me` | Current user |
| `POST/GET/DELETE /api/v1/api-keys` | Create, list and revoke API keys |
| `/docs` | OpenAPI UI |

**Auth:** send `Authorization: Bearer <jwt>`, `X-API-Key: aip_...`, or `Authorization: Bearer aip_...`.
Authenticated routes are rate limited per principal with a Redis token bucket, and return `X-RateLimit-*` headers.

**Errors** always use this shape:
`{"error": {"code", "message", "request_id", "details?"}}`.
Every response carries an `X-Request-ID` header, which is also bound to all log lines.

## Test & lint

```bash
uv run pytest                    # unit tests; integration tests are skipped if Postgres/Redis are down
uv run pytest tests/unit
uv run ruff check . && uv run ruff format --check .
uv run mypy app scripts tests
```

Integration tests use `TEST_DATABASE_URL` (default `.../platform_test`, created automatically) and Redis DB 15.

## Migrations

```bash
uv run alembic revision --autogenerate -m "add foo"
uv run alembic upgrade head
```

## Structure

```
app/
  main.py            app factory: lifespan, middleware, routers
  core/              config, logging, errors, security, rate limiting, request-id middleware, resources
  db/                declarative base + mixins (UUIDv7 id, timestamps, tenant_id), engine/session
  models/            SQLAlchemy models
  schemas/           Pydantic request/response models
  repositories/      data access (queries only)
  services/          business logic
  api/deps.py        DI: db session, settings, current principal, rate limits
  api/routes/        one module per resource
alembic/             migrations
scripts/seed.py      idempotent dev seed
tests/unit           no external services
tests/integration    real Postgres + Redis
```
