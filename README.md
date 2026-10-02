# AI Multi-Provider Platform

A self-hostable AI platform that routes requests across multiple **free LLM providers**, ingests documents asynchronously into a **GraphRAG** knowledge base, answers questions **with citations**, evaluates its own answers, and keeps conversation history **token-efficient and shareable**. The whole system lives in one monorepo.

> **Status:** 🚧 Early development. The architecture is defined and the build is starting. See the [Roadmap](#roadmap) for which parts exist yet.

---

## Features

- **Multi-provider LLM routing.** One OpenAI-compatible gateway in front of LiteLLM. Your code asks for a role such as `chat-fast`, `chat-smart`, `extract`, `summarize`, `judge` or `embed`, not a vendor model. The gateway handles:
  - routing that respects each provider's rate limits, with automatic fallback between providers
  - a circuit breaker for each provider
  - a semantic cache for repeated prompts
  - per-tenant quotas
- **Free models only.** Local models run through Ollama (Qwen 2.5, Mistral, Llama 3.2, nomic-embed). Hosted free tiers come from OpenRouter (`:free`), Groq and Gemini.
- **Asynchronous document ingestion.** Uploads go to object storage, then a job is queued on RabbitMQ with retries and a dead-letter queue. Workers process each file with LlamaIndex: parse → chunk → extract metadata → embed → extract entities and relations. Progress streams to the UI.
- **GraphRAG retrieval:**
  1. Hybrid dense + sparse vector search in Qdrant.
  2. Graph expansion over the Neo4j entity graph.
  3. Reciprocal-rank fusion and a local reranker.
  4. An answer with inline `[n]` citations that are checked against the chunks actually retrieved.
- **Optimized, shareable history:**
  - **Token budget:** each request gets a fixed budget split across a rolling summary, extracted facts, older turns similar to the question, and the most recent turns.
  - **Background compaction:** summaries and facts are updated outside the request path.
  - **Context capsules:** a conversation can be exported as a 200–600 token capsule. It can be shared as a link, used to start a new conversation on a different model, or passed to another agent.
- **Evaluation:**
  - **Online:** a sample of live answers is scored by an LLM judge for faithfulness, answer relevance, context precision and citation accuracy.
  - **Offline:** golden datasets measure the same scores, plus retrieval hit@k and how well compressed history preserves facts. CI fails when quality drops below set thresholds.
- **Observability.** OpenTelemetry traces follow a request through HTTP calls and queue messages. Data goes to Grafana, Tempo, Prometheus and Loki, with Langfuse for LLM-specific traces, prompts and scores.
- **Production concerns:**
  - multi-tenancy, JWT and API-key auth, and rate limiting
  - health and readiness probes
  - Helm charts, with KEDA scaling workers on queue depth
  - CI/CD on GitHub Actions

---

## Architecture

```
Client Apps (Next.js web · TS SDK · curl)
        │  REST + SSE
        ▼
Platform API ── auth · tenants · conversations · history · documents · sharing
   │                 │                      │                     │
   ▼                 ▼                      ▼                     ▼
AI Gateway     Document Service      Chat / RAG Orchestrator   Evaluation
   │           (MinIO + publish)     (history → retrieval → LLM)     │
   ▼                 │                                               ▼
LiteLLM              ▼                                         Eval queue → Eval workers
 ├ Ollama        RabbitMQ (retry + DLQ)
 ├ OpenRouter        ▼
 ├ Groq          Ingestion workers (LlamaIndex)
 └ Gemini         Parse → Chunk → Metadata → Embeddings → Entities/Relations
                     │                                 │
                     ▼                                 ▼
               Qdrant (vectors)              Neo4j (documents · chunks · entities · relationships)
                     └────────────────┬────────────────┘
                                      ▼
                       Hybrid retrieval + graph expansion + rerank
                                      ▼
                         AI Gateway → LLM → Answer + citations → Evaluation
```

**Supporting services:** PostgreSQL holds all records. Redis handles caching, rate limits and the recent-conversation window. MinIO stores raw files. Langfuse records LLM traces. OpenTelemetry sends data to Grafana, Tempo, Prometheus and Loki.

---

## Tech Stack

| Area | Technology |
|---|---|
| Backend services | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic |
| LLM routing | LiteLLM proxy + custom gateway |
| Models | Ollama (local), OpenRouter free, Groq free, Gemini free |
| RAG / ingestion | LlamaIndex, fastembed (sparse vectors and reranker) |
| Vector store | Qdrant |
| Knowledge graph | Neo4j |
| Messaging | RabbitMQ (aio-pika) |
| Storage | PostgreSQL, Redis, MinIO |
| Frontend | Next.js 15 (App Router), TypeScript, Tailwind, shadcn/ui |
| Observability | OpenTelemetry, Grafana, Tempo, Prometheus, Loki, Langfuse |
| Monorepo tooling | uv workspaces (Python), pnpm + Turborepo (TS), Make |
| Deploy | Docker Compose (dev), Helm + KEDA (Kubernetes), GitHub Actions |

---

## Repository Layout

```
apps/
  frontend/            Next.js app: chat, documents, graph explorer, evals, admin
  backend/             uv workspace
    services/
      api/             Platform API
      gateway/         AI Gateway (wraps LiteLLM)
      ingestion_worker/  Document pipeline + history compaction
      eval_worker/     Online evaluation
    libs/              core · db · messaging · llm_client · rag · graph · history · evals · observability
packages/              sdk-ts · ui · config (shared TS packages)
infra/                 litellm · rabbitmq · neo4j · grafana/prometheus/loki/tempo · helm
docker/                compose files and Dockerfiles
evals/                 golden datasets and CI thresholds
docs/                  architecture, ADRs, runbooks
scripts/               seeding, model pulls, SDK generation, load tests
```

---

## Getting Started

### Prerequisites
- Docker + Docker Compose
- Python 3.12 with [uv](https://docs.astral.sh/uv/)
- Node.js 22 with npm
- `make`. On Windows, install it with `winget install ezwinports.make`, or run the commands in [apps/backend/README.md](apps/backend/README.md) directly.
- Optional: a GPU for faster local Ollama models
- Optional: free API keys for OpenRouter, Groq or Gemini

### Setup
```bash
make env        # create docker/.env, apps/backend/.env, apps/frontend/.env.local
make install    # uv sync, npm ci, pre-commit hooks
make up         # start Postgres, Redis, RabbitMQ, Qdrant, Neo4j, MinIO, LiteLLM
make migrate    # apply database migrations
make seed       # default tenant + admin@example.com / admin12345
make dev        # backend :8000 + frontend :3000 with hot reload
```
Then open http://localhost:3000 and sign in.

If a host port is already in use, override it in `docker/.env`, e.g. `POSTGRES_PORT=5433`, and update `DATABASE_URL` in `apps/backend/.env` to match.

### Common commands
| Command | Description |
|---|---|
| `make up` / `make down` | Start or stop the infrastructure containers |
| `make up-all` | Run the full stack, apps included, in Docker |
| `make dev` | Run the backend and frontend locally with hot reload |
| `make migrate` / `make migration m="..."` | Apply migrations / create a new one |
| `make test` | Backend unit and integration tests, plus frontend lint |
| `make lint` / `make typecheck` | ruff and eslint / mypy and tsc |
| `make check` | Everything CI runs |
| `make help` | List all targets |

### Local endpoints (default)
| Service | URL |
|---|---|
| Web app | http://localhost:3000 |
| Platform API docs | http://localhost:8000/docs |
| AI Gateway | http://localhost:8100 |
| RabbitMQ UI | http://localhost:15672 |
| Qdrant dashboard | http://localhost:6333/dashboard |
| Neo4j Browser | http://localhost:7474 |
| Grafana | http://localhost:3001 |
| Langfuse | http://localhost:3002 |

---

## How Conversation History Stays Small

Each chat request is assembled under a token budget derived from the target model's context window. The pieces are always added in the same order, which lets providers that support prompt caching reuse the start of the prompt:

1. **System prompt.** It never changes, so providers can cache it.
2. **Memory facts.** Short facts pulled from the chat, such as preferences, decisions and documents referred to.
3. **Rolling summary.** Older turns compressed in the background, capped at about 300 tokens.
4. **Semantic recall.** Older turns that are relevant to the current question.
5. **Recent turns.** The last few turns word for word.
6. **Retrieved context.** Document chunks. These are never saved into history; only citation references are kept.

The full transcript is always kept in Postgres, so compression never loses data. Every request records the ratio of tokens saved. A dedicated eval checks that compressed history still answers questions about early turns correctly.

---

## Roadmap

- [x] **1. Foundation:** database layer and migrations, JWT and API-key auth, rate limiting, logging, readiness checks, CI, login page
- [ ] **2. AI Gateway:** LiteLLM aliases, fallbacks, circuit breaker, semantic cache, quotas
- [ ] **3. Ingestion:** upload, queue with retry and DLQ, LlamaIndex pipeline, vector store, live status
- [ ] **4. GraphRAG:** Neo4j graph, hybrid and graph retrieval, rerank, cited streaming answers
- [ ] **5. History:** context builder, compaction, semantic recall, context capsules
- [ ] **6. Evaluation:** online judge, offline datasets, CI quality gates, feedback
- [ ] **7. Frontend:** chat, documents, graph explorer, evals dashboard, admin
- [ ] **8. Production:** Helm and KEDA, dashboards, load tests, release pipeline, runbooks

---

## Notes on Free Models

Free-tier model IDs and rate limits change often. They are defined only in `infra/litellm/config.yaml`, where environment variables can override them; application code refers only to role names like `chat-fast`. The platform runs with no API keys at all, using local Ollama models only. Each free-tier key you add becomes another fallback route.

## License

TBD.
