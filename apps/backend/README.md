# Backend

A uv workspace that holds every Python service and the libraries they share.

```
libs/
  core/        platform_core: base settings, logging, error schema, request-id middleware,
               Redis token-bucket rate limiter, readiness helpers, blob stores
               (transient Redis by default, MinIO/S3 optional)
  db/          platform_db: SQLAlchemy base/mixins, models, session factory, Alembic migrations
  llm_client/  llm_client: OpenAI-compatible types + typed async GatewayClient
               (retries, streaming, judge() for structured JSON answers)
  messaging/   platform_messaging: RabbitMQ topology, publisher, retrying consumer,
               job contracts, Redis status events, trace headers
  graph/       platform_graph: Neo4j schema, tenant-scoped graph store, entity/relation extraction
  history/     platform_history: token-budgeted context builder, compaction (summary + memory),
               semantic recall of older turns, context capsules
  evals/       platform_evals: LLM-judge metrics (faithfulness, answer relevance, context
               precision, citation accuracy), hit@k/MRR, loading and storing evaluations,
               and the offline runner (`python -m platform_evals.cli`)
  rag/         platform_rag: Qdrant chunk store, BM25, hybrid retrieval, reranking (`[rerank]`),
               cited-answer prompt and citation validation; [ingest] extra adds
               parsing, chunking, BM25, metadata extraction and the ingestion pipeline
services/
  api/               platform_api: Platform API (auth, API keys, documents, LLM endpoints, usage)
  gateway/           gateway: AI Gateway (alias routing, fallbacks, circuit breaker, caching, quotas)
  ingestion_worker/  ingestion_worker: RabbitMQ consumers for the ingestion pipeline and
                     history compaction
  eval_worker/       eval_worker: RabbitMQ consumer scoring sampled answers with the LLM judge
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

The API and worker read `apps/backend/.env`; the gateway also reads `docker/.env` for the
provider API keys (`GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `NVIDIA_API_KEY`).

For local models, run `make models` (starts Ollama and pulls `qwen2.5:7b`, `mistral:7b`, `nomic-embed-text`).
With no provider keys at all, the gateway still works through Ollama alone.

## Run

```bash
uv run uvicorn platform_api.main:app --reload --port 8000
uv run uvicorn gateway.main:app --reload --port 8100 --env-file ../../docker/.env   # provider keys
uv run python -m ingestion_worker
uv run python -m eval_worker
```

`make dev` runs all four, plus the frontend.

### Platform API (`:8000`)

| Endpoint | Description |
|---|---|
| `GET /healthz`, `GET /readyz` | Liveness / readiness (DB, Redis, RabbitMQ required; gateway, upload store and Qdrant reported but optional) |
| `POST /api/v1/auth/login`, `GET /api/v1/auth/me` | JWT login, current user |
| `POST/GET/DELETE /api/v1/api-keys` | API keys |
| `POST /api/v1/llm/chat/completions` | OpenAI-compatible chat (streaming supported) |
| `POST /api/v1/llm/embeddings`, `GET /api/v1/llm/models` | Embeddings, available aliases |
| `GET /api/v1/usage/summary?hours=24` | Tenant usage by alias + today's quota |
| `GET /api/v1/admin/providers` | Provider / circuit-breaker status (admin) |
| `POST /api/v1/documents` | Upload (multipart `file`): PDF, DOCX, MD, HTML, TXT up to `MAX_UPLOAD_MB`. 201 new; 200 + `X-Duplicate` if the tenant already has the same bytes. A duplicate of a `failed` document, or of one embedded by another model, is queued again |
| `GET /api/v1/documents`, `GET/DELETE /api/v1/documents/{id}` | List, get, delete (removes chunks, graph facts and any pending upload) |
| `POST /api/v1/documents/{id}/reingest` | Re-run ingestion while the upload is still held; 409 `original_discarded` once it has been processed (upload the file again instead) |
| `GET /api/v1/documents/{id}/chunks` | Chunk preview (text, page, section) |
| `GET /api/v1/documents/{id}/events`, `GET /api/v1/documents/events` | SSE status stream for one document / the whole tenant |
| `POST/GET /api/v1/conversations`, `GET/PATCH/DELETE /api/v1/conversations/{id}` | Conversations (private to their user), with an optional `doc_ids` filter |
| `GET /api/v1/conversations/{id}/messages` | Turns, with citations resolved to the cited chunk text |
| `POST /api/v1/conversations/{id}/chat` | Grounded answer over SSE: `token`…, `citations`, `usage`, `done` |
| `GET /api/v1/graph/entities?q=`, `GET /api/v1/graph/neighborhood/{entity_id}?hops=1` | Entity search and subgraph for the explorer |
| `POST /api/v1/conversations/{id}/capsules` | Snapshot a conversation as an immutable context capsule with a signed share token |
| `GET /api/v1/capsules`, `GET /api/v1/capsules/{share_token}`, `DELETE /api/v1/capsules/{id}` | Your capsules; read-only view by share link (same workspace, unexpired) |
| `POST /api/v1/capsules/{id}/fork` | New conversation seeded with the capsule, optionally on another `alias` |
| `POST/DELETE /api/v1/messages/{id}/feedback` | Thumbs up/down (`rating` 1 or -1) and an optional comment on one of your answers |
| `GET /api/v1/evals/summary?days=30` | Mean scores, daily trend, per-model comparison, abstention rate and feedback counts (admin) |
| `GET /api/v1/evals/results?metric=&model=&source=&order=worst` | Evaluated answers, worst first (admin) |
| `GET /api/v1/evals/results/{id}` | One evaluation with the question, answer, sources and the judge's per-claim verdicts (admin) |
| `POST /api/v1/evals/messages/{id}` | Score an answer now with the judge (admin; the offline runner uses this) |

The LLM endpoints work with the OpenAI SDKs:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/api/v1/llm", api_key="aip_...")
client.chat.completions.create(model="chat-fast", messages=[{"role": "user", "content": "hi"}])
```

### AI Gateway (`:8100`, internal)

Callers authenticate with `GATEWAY_SERVICE_TOKEN` and identify the tenant with `X-Tenant-ID`.

- **Aliases** (`chat-fast`, `chat-smart`, `extract`, `summarize`, `judge`, plus `auto`; embeddings
  `embed`, `embed-gemini`, `embed-nvidia`) map to ordered deployment chains in
  [`infra/gateway/routes.yaml`](../../infra/gateway/routes.yaml). LiteLLM only translates between
  providers; it doesn't route.

  | Alias | Deployments, in fallback order |
  |---|---|
  | `chat-fast` | Groq gpt-oss-20b → NVIDIA Nemotron Bolt → Gemini Flash-Lite → OpenRouter Gemma 4 26B → Ollama Qwen 2.5 7B |
  | `chat-smart` | Gemini Flash → Groq gpt-oss-120b → NVIDIA Nemotron Lightning → Groq Qwen3 27B → OpenRouter Gemma 4 31B → NVIDIA Mistral Large 4 → Ollama Qwen 2.5 7B |
  | `extract` | Ollama Qwen 2.5 7B → NVIDIA Nemotron Bolt → Gemini Flash-Lite → Groq gpt-oss-20b → NVIDIA Nemotron Lightning |
  | `summarize` | Ollama Qwen 2.5 7B → NVIDIA Nemotron Bolt → Groq gpt-oss-20b → Gemini Flash-Lite |
  | `judge` | Groq Qwen3 27B → NVIDIA Mistral Large 4 → OpenRouter Gemma 4 31B → Ollama Mistral 7B |
  | `embed` / `embed-gemini` / `embed-nvidia` | Ollama nomic-embed-text (768-d) / Gemini embedding-001 (768-d) / NVIDIA Nemotron embed (2048-d) |

  Embedding aliases have one deployment each: vectors from different models can't be mixed, so
  there is no fallback between them.
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

Extra request fields are passed through to the provider, e.g. `input_type` on `/v1/embeddings`.

### Document ingestion

```
POST /documents -> upload held in Redis (upload:{tenant}/{doc}/file, TTL) + documents row (queued)
                -> RabbitMQ ingest.documents
ingestion_worker: parse -> chunk -> summary/keywords (`extract`) -> embed (dense via gateway EMBED_ALIAS,
                  BM25 sparse) -> Qdrant chunks_<EMBED_MODEL_NAME> -> entities/relations -> Neo4j
                  -> status ready | failed -> upload deleted
status changes: Postgres (source of truth) + Redis pub/sub -> SSE
```

- **Statuses:** `queued -> parsing -> chunking -> embedding -> graphing -> ready | failed`.
- **Uploads aren't kept.** With the default `BLOB_STORE=redis`, the file lives in Redis only until the
  document is `ready` or has failed for good, and at most `UPLOAD_RETENTION_HOURS` (24). Only chunks,
  embeddings, graph facts and metadata remain, and the worker records `details.embed_model`.
  `BLOB_STORE=minio` (with the compose `s3` profile and the `S3_*` settings) keeps originals instead.
- **Failures:** corrupt or unsupported files, quota exhaustion and other 4xx errors fail at once (dead-lettered to `ingest.dlq` with `x-error` headers). Transient errors such as the gateway, Qdrant or storage being down are retried after 10s, 1m and 10m, then dead-lettered. A worker that dies mid-job leaves its message unacked, so RabbitMQ redelivers it; after 5 redeliveries it is dead-lettered.
- **Idempotent:** chunk ids derive from `(doc_id, chunk_index)` and reprocessing replaces the document's points. Uploads are deduplicated per tenant by SHA-256.
- **Chunks:** markdown, DOCX and HTML split on headings first, so each chunk carries its section path. PDF chunks never span pages. Each chunk is embedded with its document title and section as a header.

### Embedding models

The API (queries) and the worker (chunks, conversation turns) must use the same model:

| Setting | Purpose |
|---|---|
| `EMBED_ALIAS` | Gateway alias: `embed` (Ollama), `embed-gemini` or `embed-nvidia` |
| `EMBED_MODEL_NAME` | Names the Qdrant collections (`chunks_<name>`, `conversation_turns_<name>`) and is recorded per document |
| `EMBED_DOCUMENT_PREFIX` / `EMBED_QUERY_PREFIX` | Text prefixes for models that want them (nomic: `search_document: ` / `search_query: `); empty for Gemini and NVIDIA |
| `EMBED_DOCUMENT_INPUT_TYPE` / `EMBED_QUERY_INPUT_TYPE` | `input_type` for asymmetric models; NVIDIA Nemotron needs `passage` / `query` |

Switching models starts from empty collections. Upload documents again to embed them with the new
model; the old collection can then be deleted in Qdrant.

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

### Conversation history

Every chat request builds its prompt within a token budget: `min(alias context window, HISTORY_MAX_CONTEXT_TOKENS)`, split as

| system | memory facts | rolling summary | semantic recall | recent turns | documents | answer |
|---|---|---|---|---|---|---|
| 5% | ≤5% | ≤10% | ≤10% | ≤20% | ~35% + unused | 15% |

- **Compression** before slotting: citation markup stripped, long code blocks collapsed to `[code: lang, n lines, msg#id]`, a repeated question supersedes the earlier exchange, and retrieved document text is never replayed (messages store only answers and chunk ids).
- **Stable order** (system → memory + summary → recall → recent turns), so the long-lived prefix stays identical between requests.
- **Recent window:** at most `HISTORY_MAX_RECENT_MESSAGES` (10) newest messages, verbatim.
- **Compaction** runs in the worker (`history.compact` queue) once 10 messages are unsummarized or they exceed `HISTORY_COMPACT_THRESHOLD` (1,500 tokens). The `summarize` alias folds older turns into a summary capped at 300 tokens and returns a validated memory patch, and turn pairs are embedded into Qdrant `conversation_turns_<model>` for recall. The newest `HISTORY_KEEP_RECENT` (6) messages stay verbatim.
- **Capsules** hold the summary, memory facts, key Q/A pairs and citation refs (typically 200–600 tokens). They can be shared by signed link, forked onto another model, or attached to one request with `context_capsule_id` for agent handoff.
- **Metrics:** each answer's `usage.history` records `raw_history_tokens` (full replay) against `sent_history_tokens`; `/usage/summary` reports `history_tokens_saved_ratio`.

### Evaluation

```
chat answer saved -> sampled (EVAL_SAMPLE_RATE, default 0.2) -> RabbitMQ eval.answers
eval_worker: question + answer + the numbered sources (re-read from Qdrant by chunk id)
             -> 4 judge calls (`judge` alias) -> eval_results (one row per answer)
```

| Metric | How it's scored |
|---|---|
| Faithfulness | The judge splits the answer into claims and checks each against the sources; share supported |
| Answer relevance | 1–5 rating of how directly the answer addresses the question, scaled to 0–1; skipped for "I don't know" |
| Context precision | The judge marks each source relevant or not; average precision over the ranking |
| Citation accuracy | Each sentence carrying `[n]` is checked against the sources it cites; numbers outside the sources count as wrong |
| hit@k, MRR (offline) | Rank of the first source from the expected document |

- **A different judge.** The judge request carries `X-Gateway-Avoid-Providers` with the answering
  model's provider, so the gateway tries other providers first and uses that one only as a last resort.
- **Robust to bad JSON.** A judge reply that fails validation leaves that one metric empty, with the
  error in `details`; if every metric fails the job is retried. Gateway outages retry via the queue.
- **Feedback.** Thumbs up/down live in `message_feedback`, are returned with each message, and are
  counted per answering model in the summary.
- **Offline.** `make eval` (`python -m platform_evals.cli`) uploads `evals/corpus`, asks every question in
  `evals/datasets/golden.jsonl` in a fresh conversation, has the API judge each answer
  (`source=offline`, one `run_id` per run), adds hit@k, MRR, expected-term recall and abstention
  accuracy, and writes `evals/reports/<run id>.json`. It exits 1 when a mean is below
  `evals/thresholds.yaml`. `suite=history` replays `evals/datasets/history_*.jsonl` and checks that
  facts from early turns are still recalled. `.github/workflows/evals.yml` runs it on PRs that touch
  retrieval, history, evals or model routing (needs provider keys as repository secrets).

## Test & lint

Each member runs its own tests:

```bash
(cd libs/llm_client && uv run pytest)
(cd services/gateway && uv run pytest)    # no services needed (fake LiteLLM + fakeredis)
(cd services/api && uv run pytest)        # integration tests skip if Postgres/Redis are down
(cd libs/messaging && uv run pytest)      # retry/DLQ/redelivery against real RabbitMQ (skips if down)
(cd libs/graph && uv run pytest)          # extraction; graph store against real Neo4j (skips if down)
(cd libs/rag && uv run pytest)            # parsing, chunking, BM25, pipeline, retrieval, citations, golden corpus
(cd services/ingestion_worker && uv run pytest)  # end-to-end: API -> RabbitMQ -> worker -> Qdrant/Neo4j
(cd libs/history && uv run pytest)        # context builder, compaction, capsules
(cd libs/evals && uv run pytest)          # judge metrics and scoring
(cd services/eval_worker && uv run pytest)  # scorer against real Postgres (skips if down)
uv run ruff check . && uv run ruff format --check .
```

Or `make test`, `make lint` and `make typecheck` from the repo root.
API integration tests use `TEST_DATABASE_URL` (default `.../platform_test`, created automatically) and Redis DB 15; the ingestion end-to-end tests use `TEST_INGEST_DATABASE_URL` (`.../platform_test_ingest`), Redis DB 14 (flushed afterwards), and throwaway queues and collections. The eval worker tests use
`TEST_EVAL_DATABASE_URL` (`.../platform_test_eval`).

Against a running stack with an LLM route, `make eval` scores the golden dataset (see
[Evaluation](#evaluation)), `make golden` asks the golden questions in
`evals/datasets/golden.jsonl`, and `make history-check` runs a 40-turn conversation that checks
early facts survive compression and reports the tokens saved.

## Migrations

```bash
uv run alembic -c libs/db/alembic.ini revision --autogenerate -m "add foo"
uv run alembic -c libs/db/alembic.ini upgrade head
```

In Docker, the one-shot `migrate` service applies migrations before the API and gateway start.
