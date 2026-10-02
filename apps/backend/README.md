# Backend

A uv workspace that holds every Python service and the libraries they share.

```
libs/
  core/        platform_core: base settings, logging, error schema, request-id middleware,
               Redis token-bucket rate limiter, readiness helpers
  db/          platform_db: SQLAlchemy base/mixins, models, session factory, Alembic migrations
  llm_client/  llm_client: OpenAI-compatible types + typed async GatewayClient
               (retries, streaming, judge() for structured JSON answers)
services/
  api/         platform_api: Platform API (auth, API keys, public LLM endpoints, usage)
  gateway/     gateway: AI Gateway (alias routing, fallbacks, circuit breaker, caching, quotas)
```

## Setup

```bash
# from repo root: start Postgres, Redis, RabbitMQ, Qdrant, LiteLLM, ...
make up

cd apps/backend
cp .env.example .env
uv sync --all-packages
uv run alembic -c libs/db/alembic.ini upgrade head
uv run python -m platform_api.cli.seed      # default tenant + admin@example.com / admin12345
```

Or from the repo root: `make up && make migrate && make seed`.

For local models, run `make models` (starts Ollama and pulls `qwen2.5:7b`, `mistral:7b`, `nomic-embed-text`).
With no provider keys at all, the gateway still works through Ollama alone.

## Run

```bash
uv run uvicorn platform_api.main:app --reload --port 8000
uv run uvicorn gateway.main:app --reload --port 8100 --env-file ../../docker/.env   # provider keys
```

`make dev` runs both, plus the frontend.

### Platform API (`:8000`)

| Endpoint | Description |
|---|---|
| `GET /healthz`, `GET /readyz` | Liveness / readiness (DB, Redis, RabbitMQ required; gateway reported but optional) |
| `POST /api/v1/auth/login`, `GET /api/v1/auth/me` | JWT login, current user |
| `POST/GET/DELETE /api/v1/api-keys` | API keys |
| `POST /api/v1/llm/chat/completions` | OpenAI-compatible chat (streaming supported) |
| `POST /api/v1/llm/embeddings`, `GET /api/v1/llm/models` | Embeddings, available aliases |
| `GET /api/v1/usage/summary?hours=24` | Tenant usage by alias + today's quota |
| `GET /api/v1/admin/providers` | Provider / circuit-breaker status (admin) |

The LLM endpoints work with the OpenAI SDKs:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/api/v1/llm", api_key="aip_...")
client.chat.completions.create(model="chat-fast", messages=[{"role": "user", "content": "hi"}])
```

### AI Gateway (`:8100`, internal)

Callers authenticate with `GATEWAY_SERVICE_TOKEN` and identify the tenant with `X-Tenant-ID`.

- **Aliases** (`chat-fast`, `chat-smart`, `extract`, `summarize`, `judge`, `embed`, plus `auto`) map
  to ordered deployment chains in [`infra/gateway/routes.yaml`](../../infra/gateway/routes.yaml).
  LiteLLM only translates between providers; it doesn't route.
- **Fallback:** on 5xx, 429, auth errors, timeouts or 404s, the gateway moves to the next deployment.
  400/422 responses are returned to the caller as-is. Deployments whose API key is unset are skipped.
- **Per-deployment RPM budgets** keep calls inside each provider's free-tier limits.
- **Circuit breaker** per provider, stored in Redis: it opens after N failures in a window, then lets
  one half-open probe through once the cooldown ends.
- **Cache:** exact matches in Redis, then semantic matches (Qdrant, cosine ≥ 0.95). Entries are scoped by
  tenant, alias and generation params. Send `Cache-Control: no-cache` to bypass.
- **Quotas:** a daily token budget per tenant (`tenants.daily_token_quota`, or the default), returning 429 `quota_exceeded` when spent.
- **Guardrails:** prompt-injection heuristics (`flag`/`block`) and optional PII redaction.
- **Usage:** every request is written to `llm_usage`.

Response headers report how each request was served: `X-Gateway-Alias`, `-Deployment`, `-Provider`, `-Cache`, `-Fallbacks`, `-Guardrail`.

## Test & lint

Each member runs its own tests:

```bash
(cd libs/llm_client && uv run pytest)
(cd services/gateway && uv run pytest)    # no services needed (fake LiteLLM + fakeredis)
(cd services/api && uv run pytest)        # integration tests skip if Postgres/Redis are down
uv run ruff check . && uv run ruff format --check .
```

Or `make test`, `make lint` and `make typecheck` from the repo root.
API integration tests use `TEST_DATABASE_URL` (default `.../platform_test`, created automatically) and Redis DB 15.

## Migrations

```bash
uv run alembic -c libs/db/alembic.ini revision --autogenerate -m "add foo"
uv run alembic -c libs/db/alembic.ini upgrade head
```

In Docker, the one-shot `migrate` service applies migrations before the API and gateway start.
