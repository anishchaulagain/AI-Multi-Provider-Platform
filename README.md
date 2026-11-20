# AI Multi-Provider Platform

A self-hostable AI platform that routes requests across multiple **free LLM providers**, ingests documents asynchronously into a **GraphRAG** knowledge base, answers questions **with citations**, evaluates its own answers, and keeps conversation history **token-efficient and shareable**. The whole system lives in one monorepo.

---

## Features

- **Multi-provider LLM routing.** One OpenAI-compatible gateway sits in front of LiteLLM. Code asks for a role (`chat-fast`, `chat-smart`, `extract`, `summarize`, `judge`, `embed`), never a vendor model. For each role the gateway provides:
  - an ordered fallback chain across providers
  - per-model requests-per-minute budgets that match each free tier
  - a circuit breaker per provider
  - exact and semantic response caching
  - per-tenant daily token quotas and prompt-injection checks
- **Free models only.** Hosted free tiers from Groq, Gemini, OpenRouter (`:free`) and NVIDIA (build.nvidia.com), plus local models through Ollama. The platform runs with no API keys at all; every key you add becomes another fallback route.
- **Asynchronous document ingestion.**
  - Uploads are queued on RabbitMQ, with timed retries and a dead-letter queue.
  - Workers parse, chunk, extract a summary and keywords, embed, and extract entities and relations.
  - Status streams to the UI over SSE.
- **Uploads are not stored.** The original file is held in Redis only until its chunks are embedded, then deleted (after 24 hours at the latest). Users see the document's name, status and chunks, and answers cite it, but nobody can download the file. To redo a document, upload it again.
- **GraphRAG answers with citations:**
  1. The question is rewritten into a standalone query.
  2. Qdrant runs a hybrid dense + BM25 search.
  3. Neo4j expands the query through the entity graph.
  4. The results are fused with reciprocal-rank fusion and reranked by a local cross-encoder.
  5. The answer streams with inline `[n]` citations. Citations that don't match a retrieved chunk are dropped.
- **Token-efficient, shareable history:**
  - Each request fits a token budget split across memory facts, a rolling summary, recalled older turns and recent turns.
  - Compaction runs in the background, off the request path.
  - A conversation can be saved as a 200–600 token **context capsule**. Share it by signed link, continue it on another model, or attach it to an API call.
- **Web app.** Pages for sign-in, chat with clickable citations, document upload with live status, an LLM playground, API keys, and shared-capsule views.
- **Evaluation:** an LLM judge scores a sample of live answers for faithfulness, relevance and citation accuracy. Offline golden datasets back CI quality gates. Users can give feedback on answers.
- **Observability:** OpenTelemetry traces across HTTP and queues; Grafana, Tempo, Prometheus and Loki; Langfuse for LLM traces.
- **Production:** Helm charts, KEDA scaling of workers on queue depth, load tests, and a release pipeline.

---

## Architecture

```
Next.js web app · OpenAI SDKs · curl
        │  REST + SSE
        ▼
Platform API ── auth · tenants · documents · conversations · history · capsules
   │                  │                         │
   ▼                  ▼                         ▼
AI Gateway      Upload (bytes → Redis,     Chat orchestrator
   │             job → RabbitMQ)           (history → retrieval → LLM → citations)
   ▼                  │
LiteLLM               ▼
 ├ Groq          Ingestion worker ── parse → chunk → metadata → embed → entities/relations
 ├ Gemini              │                 then deletes the uploaded file
 ├ OpenRouter          ├──────────────► Qdrant  (dense + BM25 chunks, conversation turns)
 ├ NVIDIA              └──────────────► Neo4j   (documents · chunks · entities · relations)
 └ Ollama
                 History compaction (same worker, own queue) → summary + memory facts
```

**Stores:**
- **PostgreSQL** holds all records.
- **Redis** handles rate limits, caches, circuit-breaker state, status events and pending uploads.
- **Qdrant** holds vectors.
- **Neo4j** holds the knowledge graph.

MinIO/S3 is optional: set `BLOB_STORE=minio` to keep the original files.

---

## Tech Stack

| Area | Technology |
|---|---|
| Backend services | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic |
| LLM routing | LiteLLM proxy as a provider adapter + custom AI Gateway |
| Models | Groq, Gemini, OpenRouter `:free`, NVIDIA build.nvidia.com, Ollama (local) |
| RAG / ingestion | LlamaIndex, PyMuPDF, fastembed (BM25 + cross-encoder reranker) |
| Vector store | Qdrant |
| Knowledge graph | Neo4j 5 |
| Messaging | RabbitMQ 4 (aio-pika), quorum queues, TTL retry queues, DLQ |
| Storage | PostgreSQL 17, Redis 7 |
| Frontend | Next.js 16 (App Router), React 19, TypeScript, Tailwind 4, shadcn/ui |
| Tooling | uv workspace, npm, Make, ruff, mypy, pytest, GitHub Actions |
| Observability | OpenTelemetry, Grafana, Tempo, Prometheus, Loki, Langfuse |
| Deploy | Docker Compose, Helm + KEDA (Kubernetes) |

---

## Repository Layout

```
apps/
  frontend/              Next.js web app (see apps/frontend/README.md)
  backend/               uv workspace (see apps/backend/README.md)
    services/
      api/               Platform API
      gateway/           AI Gateway (wraps LiteLLM)
      ingestion_worker/  Document pipeline + history compaction
    libs/                core · db · llm_client · messaging · graph · rag · history
infra/
  litellm/config.yaml    one LiteLLM model per provider deployment
  gateway/routes.yaml    deployments, RPM budgets and alias fallback chains
  rabbitmq/ neo4j/       broker definitions, graph schema
docker/                  compose files, Dockerfiles, .env.example (see docker/README.md)
evals/                   golden corpus (corpus/) and questions (datasets/golden.jsonl)
scripts/                 golden_smoke.py, history_smoke.py, pull-ollama-models.sh
```

---

## Getting Started

### Prerequisites
- Docker with Docker Compose
- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 22 and npm
- `make`. On Windows, install it with `winget install ezwinports.make` and use Git Bash, or run the commands from [apps/backend/README.md](apps/backend/README.md) directly.
- At least one LLM route:
  - a free API key for Groq, Gemini, OpenRouter or NVIDIA, or
  - local Ollama: `make models` pulls the models, and a GPU helps.

### Setup
```bash
make env        # create docker/.env, apps/backend/.env, apps/frontend/.env.local (never overwrites)
#   put provider keys in docker/.env (GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, NVIDIA_API_KEY)
make install    # uv sync, npm ci, pre-commit hooks
make up         # start Postgres, Redis, RabbitMQ, Qdrant, Neo4j, LiteLLM
make migrate    # apply database migrations
make seed       # default tenant + admin@example.com / admin12345
make dev        # API :8000, gateway :8100, ingestion worker, frontend :3000 (hot reload)
```
Then open http://localhost:3000 and sign in.

`make up-all` runs everything, apps included, in Docker; migrations then run automatically.

If a host port is already taken, override it in `docker/.env` (for example `POSTGRES_PORT=5433`) and update `DATABASE_URL` in `apps/backend/.env` to match.

### Choosing the embedding model
The API and the ingestion worker must use the same embedding model. Each model gets its own Qdrant collection, `chunks_<EMBED_MODEL_NAME>`. Set these in `docker/.env` for Docker, or in `apps/backend/.env` for `make dev`:

| Option | Settings |
|---|---|
| Ollama `nomic-embed-text` (default, local, 768-d) | `EMBED_ALIAS=embed` · `EMBED_MODEL_NAME=nomic-embed-text` |
| Gemini `gemini-embedding-001` (768-d) | `EMBED_ALIAS=embed-gemini` · `EMBED_MODEL_NAME=gemini-embedding-001` · empty `EMBED_DOCUMENT_PREFIX` and `EMBED_QUERY_PREFIX` |
| NVIDIA Nemotron `nemotron-3.5-embed-1b` (2048-d) | `EMBED_ALIAS=embed-nvidia` · `EMBED_MODEL_NAME=nemotron-3.5-embed-1b` · empty prefixes · `EMBED_DOCUMENT_INPUT_TYPE=passage` · `EMBED_QUERY_INPUT_TYPE=query` |

After switching models, upload your documents again. A document embedded with a different model, or one that failed, is processed again; uploading an unchanged, up-to-date document does nothing.

### Common commands
| Command | Description |
|---|---|
| `make up` / `make down` | Start or stop the infrastructure containers |
| `make up-all` / `make docker-dev` | Full stack in Docker / with hot reload |
| `make dev` | API, gateway, worker and frontend locally with hot reload |
| `make models` | Start Ollama and pull the local models |
| `make migrate` / `make migration m="..."` | Apply migrations / create a new one |
| `make test` / `make test-unit` | All tests / only those that need no running services |
| `make lint` / `make typecheck` / `make check` | ruff + eslint / mypy + tsc / everything CI runs |
| `make golden` | Ask the golden questions against the running stack |
| `make history-check` | 40-turn conversation test of history compression |
| `make help` | List all targets |

### Local endpoints
| Service | URL |
|---|---|
| Web app | http://localhost:3000 |
| Platform API docs | http://localhost:8000/docs |
| AI Gateway docs | http://localhost:8100/docs |
| LiteLLM | http://localhost:4000 |
| RabbitMQ UI | http://localhost:15672 |
| Qdrant dashboard | http://localhost:6333/dashboard |
| Neo4j Browser | http://localhost:7474 |

---

## How Conversation History Stays Small

Each chat request is assembled under a token budget: the target model's context window, capped at `HISTORY_MAX_CONTEXT_TOKENS`. The pieces are always added in the same order, so providers that support prompt caching can reuse the start of the prompt:

1. **System prompt.** It never changes.
2. **Memory facts.** Short facts the user stated, such as preferences, decisions and documents referred to.
3. **Rolling summary.** Older turns compressed in the background, capped at 300 tokens.
4. **Semantic recall.** Older turns that are relevant to the current question.
5. **Recent turns.** At most the last 10 messages, word for word.
6. **Retrieved context.** Document chunks. These are never saved into history; only citation references are kept.

The full transcript always stays in Postgres, so compression never loses data. Every answer records how many history tokens were sent and how many a full replay would have needed. In the 40-turn test (`make history-check`), compression saved about 56% of history tokens, and questions about turn 2 were still answered correctly.

---

## Phases

1. **Foundation:** database and migrations, JWT and API-key auth, rate limiting, logging, readiness checks, CI, login page
2. **AI Gateway:** aliases, fallbacks, RPM budgets, circuit breaker, semantic cache, quotas, guardrails
3. **Ingestion:** upload, queue with retry and DLQ, LlamaIndex pipeline, Qdrant, live status
4. **GraphRAG:** Neo4j graph, hybrid and graph retrieval, rerank, cited streaming answers, chat UI
5. **History:** context builder, compaction, semantic recall, memory facts, context capsules
6. **Evaluation:** online judge, offline datasets, CI quality gates, feedback
7. **Observability:** OpenTelemetry, Grafana/Tempo/Prometheus/Loki, Langfuse, dashboards
8. **Production:** Helm and KEDA, load tests, release pipeline, runbooks

---

## Notes on Free Models

Free-tier model IDs and rate limits change often:
- Deployments are defined in [`infra/litellm/config.yaml`](infra/litellm/config.yaml), which also shows how to list the models each provider's key can use.
- They are chained into aliases in [`infra/gateway/routes.yaml`](infra/gateway/routes.yaml).
- Application code refers only to aliases.

A deployment whose API key is unset is skipped. NVIDIA's model list includes many models a free key can't call, so test a model before adding it.

## License

TBD.
