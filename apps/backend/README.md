# Backend

A uv workspace that holds every Python service and the libraries they share.

```
libs/
  core/        platform_core: base settings, logging, error schema, request-id middleware,
               Redis token-bucket rate limiter, readiness helpers
  db/          platform_db: SQLAlchemy base/mixins, models, session factory, Alembic migrations
  llm_client/  llm_client: OpenAI-compatible types + typed async GatewayClient
               (retries, streaming, judge() for structured JSON answers)
  messaging/   platform_messaging: RabbitMQ topology, publisher, retrying consumer,
               job contracts, Redis status events, trace headers
  graph/       platform_graph: Neo4j schema, tenant-scoped graph store, entity/relation extraction
  rag/         platform_rag: Qdrant chunk store, BM25, hybrid retrieval, reranking (`[rerank]`),
               cited-answer prompt and citation validation; [ingest] extra adds
               parsing, chunking, BM25, metadata extraction and the ingestion pipeline
services/
  api/               platform_api: Platform API (auth, API keys, documents, LLM endpoints, usage)
  gateway/           gateway: AI Gateway (alias routing, fallbacks, circuit breaker, caching, quotas)
  ingestion_worker/  ingestion_worker: RabbitMQ consumer running the ingestion pipeline
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
uv run python -m ingestion_worker
```

`make dev` runs all three, plus the frontend.

### Platform API (`:8000`)

| Endpoint | Description |
|---|---|
| `GET /healthz`, `GET /readyz` | Liveness / readiness (DB, Redis, RabbitMQ required; gateway, storage and Qdrant reported but optional) |
| `POST /api/v1/auth/login`, `GET /api/v1/auth/me` | JWT login, current user |
| `POST/GET/DELETE /api/v1/api-keys` | API keys |
| `POST /api/v1/llm/chat/completions` | OpenAI-compatible chat (streaming supported) |
| `POST /api/v1/llm/embeddings`, `GET /api/v1/llm/models` | Embeddings, available aliases |
| `GET /api/v1/usage/summary?hours=24` | Tenant usage by alias + today's quota |
| `GET /api/v1/admin/providers` | Provider / circuit-breaker status (admin) |
| `POST /api/v1/documents` | Upload (multipart `file`): PDF, DOCX, MD, HTML, TXT up to `MAX_UPLOAD_MB`. 201 new, 200 + `X-Duplicate` if the tenant already has the same bytes |
| `GET /api/v1/documents`, `GET/DELETE /api/v1/documents/{id}` | List, get, delete (removes chunks and the stored file) |
| `POST /api/v1/documents/{id}/reingest` | Re-run ingestion for a `ready` or `failed` document |
| `GET /api/v1/documents/{id}/chunks` | Chunk preview (text, page, section) |
| `GET /api/v1/documents/{id}/events`, `GET /api/v1/documents/events` | SSE status stream for one document / the whole tenant |
| `POST/GET /api/v1/conversations`, `GET/PATCH/DELETE /api/v1/conversations/{id}` | Conversations (private to their user), with an optional `doc_ids` filter |
| `GET /api/v1/conversations/{id}/messages` | Turns, with citations resolved to the cited chunk text |
| `POST /api/v1/conversations/{id}/chat` | Grounded answer over SSE: `token`…, `citations`, `usage`, `done` |
| `GET /api/v1/graph/entities?q=`, `GET /api/v1/graph/neighborhood/{entity_id}?hops=1` | Entity search and subgraph for the explorer |

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

### Document ingestion

```
POST /documents -> MinIO {tenant}/{doc}/file + documents row (queued) -> RabbitMQ ingest.documents
ingestion_worker: parse -> chunk -> summary/keywords (`extract`) -> embed (dense via gateway `embed`,
                  BM25 sparse) -> Qdrant chunks_<EMBED_MODEL_NAME>  -> status ready | failed
status changes: Postgres (source of truth) + Redis pub/sub -> SSE
```

- **Statuses:** `queued -> parsing -> chunking -> embedding -> ready | failed`.
- **Failures:** corrupt or unsupported files, quota exhaustion and other 4xx errors fail at once (dead-lettered to `ingest.dlq` with `x-error` headers). Transient errors such as the gateway, Qdrant or storage being down are retried after 10s, 1m and 10m, then dead-lettered. A worker that dies mid-job leaves its message unacked, so RabbitMQ redelivers it; after 5 redeliveries it is dead-lettered.
- **Idempotent:** chunk ids derive from `(doc_id, chunk_index)` and a re-ingest replaces the document's points. Uploads are deduplicated per tenant by SHA-256.
- **Chunks:** markdown, DOCX and HTML split on headings first, so each chunk carries its section path. PDF chunks never span pages. Each chunk is embedded with its document title and section as a header.

### Retrieval and cited answers

```
question -> rewrite to a standalone query (chat-fast, last two turns)
         -> Qdrant dense + BM25, server-side RRF (top 30) ┐
         -> Neo4j: full-text match entities -> 1-2 hops  ┘-> RRF -> cross-encoder rerank
         -> top 8 within the token budget, + neighboring chunks -> numbered [n] sources + graph facts
         -> chat-smart streams the answer -> citations parsed; numbers outside the sources dropped
```

- **Graph:** the worker's `graphing` stage writes `(Document)-[:HAS_CHUNK]->(Chunk)-[:MENTIONS]->(Entity)-[:RELATES {type, doc_id}]->(Entity)` and `[:NEXT]` chain edges. Entities with the same normalized name and type are merged. Facts carry their `doc_id`, so deleting or re-ingesting a document removes exactly its facts, and entities nothing mentions anymore are deleted.
- **Persistence:** messages store citations as chunk ids only (never context text), plus the retrieval trace (rewritten query, ranks and scores, graph seeds and facts, timings) and token usage.
- **Degrading gracefully:** if Neo4j is down, answers come from vectors alone; if the reranker fails, the fused order is kept. Both cases are recorded in the trace.

## Test & lint

Each member runs its own tests:

```bash
(cd libs/llm_client && uv run pytest)
(cd services/gateway && uv run pytest)    # no services needed (fake LiteLLM + fakeredis)
(cd services/api && uv run pytest)        # integration tests skip if Postgres/Redis are down
(cd libs/messaging && uv run pytest)      # retry/DLQ/redelivery against real RabbitMQ (skips if down)
(cd libs/graph && uv run pytest)          # extraction; graph store against real Neo4j (skips if down)
(cd libs/rag && uv run pytest)            # parsing, chunking, BM25, pipeline, retrieval, citations, golden corpus
(cd services/ingestion_worker && uv run pytest)  # end-to-end: API -> RabbitMQ -> worker -> Qdrant
uv run ruff check . && uv run ruff format --check .
```

Or `make test`, `make lint` and `make typecheck` from the repo root.
API integration tests use `TEST_DATABASE_URL` (default `.../platform_test`, created automatically) and Redis DB 15; the ingestion end-to-end tests use `TEST_INGEST_DATABASE_URL` (`.../platform_test_ingest`), Redis DB 14, and throwaway queues, bucket and collection.

## Migrations

```bash
uv run alembic -c libs/db/alembic.ini revision --autogenerate -m "add foo"
uv run alembic -c libs/db/alembic.ini upgrade head
```

In Docker, the one-shot `migrate` service applies migrations before the API and gateway start.
