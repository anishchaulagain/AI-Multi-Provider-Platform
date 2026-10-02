# syntax=docker/dockerfile:1
# One image recipe for every Python service in the uv workspace.
# Build context: apps/backend
#   --build-arg PACKAGE=platform-api  --build-arg APP_MODULE=platform_api.main:app
#   --build-arg PACKAGE=gateway       --build-arg APP_MODULE=gateway.main:app --build-arg PORT=8100

ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS base
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONUNBUFFERED=1
WORKDIR /app

# ---- builder: non-editable install of one package + its workspace deps ----
FROM base AS builder
ARG PACKAGE
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package ${PACKAGE}

# ---- dev: editable install with dev tools; source is bind-mounted by compose.dev.yml ----
FROM base AS dev
ARG PACKAGE
ARG APP_MODULE
ARG PORT=8000
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package ${PACKAGE}
ENV PATH="/app/.venv/bin:$PATH" APP_MODULE=${APP_MODULE} PORT=${PORT}
EXPOSE ${PORT}
CMD ["sh", "-c", "exec uvicorn $APP_MODULE --host 0.0.0.0 --port $PORT --reload --reload-dir /app/services --reload-dir /app/libs"]

# ---- runtime: just the virtualenv (+ migrations), no source tree or build tools ----
FROM ${PYTHON_IMAGE} AS runtime
ARG APP_MODULE
ARG PORT=8000
RUN useradd --system --uid 1001 app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/libs/db/alembic /app/migrations/alembic
COPY --from=builder --chown=app:app /app/libs/db/alembic.ini /app/migrations/alembic.ini
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    APP_MODULE=${APP_MODULE} \
    PORT=${PORT}
USER app
EXPOSE ${PORT}
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ[\"PORT\"]}/healthz')"
CMD ["sh", "-c", "exec uvicorn $APP_MODULE --host 0.0.0.0 --port $PORT"]
