# Docker

| File | Purpose |
|---|---|
| `compose.yml` | Base stack: apps, LiteLLM, Ollama, data stores. Publishes **no** host ports. |
| `compose.ports.yml` | Host port mappings for every service (override via `.env`). |
| `compose.dev.yml` | Dev overrides: hot reload with bind-mounted source. |
| `python.Dockerfile` | One image recipe for every Python service in the uv workspace (`--build-arg PACKAGE=... APP_MODULE=...`; multi-stage `dev` / `runtime`). |
| `frontend.Dockerfile` | Next.js image (standalone output, multi-stage: `dev` / `runtime`). |
| `.env.example` | Credentials, optional free-tier LLM keys, host ports. |

## Usage

Run from the repo root:

```bash
cp docker/.env.example docker/.env

# Full stack with ports
docker compose -f docker/compose.yml -f docker/compose.ports.yml up -d --build

# Dev mode (hot reload)
docker compose -f docker/compose.yml -f docker/compose.ports.yml -f docker/compose.dev.yml up --build

# Include local Ollama, then pull models
docker compose -f docker/compose.yml -f docker/compose.ports.yml --profile llm-local up -d
docker compose -f docker/compose.yml exec ollama ollama pull qwen2.5:7b
docker compose -f docker/compose.yml exec ollama ollama pull nomic-embed-text

# Only infrastructure (run the apps locally instead)
docker compose -f docker/compose.yml -f docker/compose.ports.yml up -d postgres redis rabbitmq qdrant neo4j minio litellm

# Stop / stop and wipe volumes
docker compose -f docker/compose.yml down
docker compose -f docker/compose.yml down -v
```

Leaving out `compose.ports.yml` keeps every service on the internal Docker network only.

## Default ports

| Service | Host port | URL |
|---|---|---|
| Frontend | 3000 | http://localhost:3000 |
| Backend | 8000 | http://localhost:8000/docs |
| AI Gateway | 8100 | http://localhost:8100/docs |
| LiteLLM | 4000 | http://localhost:4000 |
| Ollama | 11434 | http://localhost:11434 |
| Postgres | 5432 | — |
| Redis | 6379 | — |
| RabbitMQ | 5672 / 15672 | http://localhost:15672 |
| Qdrant | 6333 / 6334 | http://localhost:6333/dashboard |
| Neo4j | 7474 / 7687 | http://localhost:7474 |
| MinIO | 9000 / 9001 | http://localhost:9001 |
