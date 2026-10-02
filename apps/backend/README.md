# Backend

FastAPI backend for the AI Multi-Provider Platform.

## Setup

```bash
cd apps/backend
cp .env.example .env
uv sync
```

## Run

```bash
uv run uvicorn app.main:app --reload --port 8000
```

- Health check: http://localhost:8000/api/v1/health
- API docs: http://localhost:8000/docs

## Test & lint

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

## Structure

```
app/
  main.py          # app factory, middleware, router mounting
  core/config.py   # settings loaded from env / .env
  api/router.py    # aggregates v1 routes
  api/routes/      # one module per resource
tests/
```
