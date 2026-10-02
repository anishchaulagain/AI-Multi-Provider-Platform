"""App-level behaviour that needs no running dependencies."""

from collections.abc import AsyncGenerator

import httpx
import pytest

from app.main import create_app
from tests.conftest import make_client, make_settings

# Point every dependency at a closed port so readiness fails fast.
UNREACHABLE = {
    "database_url": "postgresql+asyncpg://x:x@127.0.0.1:1/x",
    "redis_url": "redis://127.0.0.1:1/0",
    "rabbitmq_url": "amqp://x:x@127.0.0.1:1/",
}


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient]:
    app = create_app(make_settings(**UNREACHABLE))
    async for c in make_client(app):
        yield c


async def test_healthz(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_request_id_is_generated_and_echoed(client: httpx.AsyncClient) -> None:
    generated = await client.get("/healthz")
    assert generated.headers["x-request-id"]

    echoed = await client.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert echoed.headers["x-request-id"] == "abc123"


async def test_readyz_reports_unavailable_dependencies(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {"database": "error", "redis": "error", "rabbitmq": "error"}


async def test_unauthenticated_request_uses_error_schema(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "unauthorized"
    assert error["request_id"] == response.headers["x-request-id"]


async def test_validation_error_uses_error_schema(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/auth/login", json={})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert {tuple(d["loc"]) for d in error["details"]} >= {
        ("body", "email"),
        ("body", "password"),
    }


async def test_unknown_route_uses_error_schema(client: httpx.AsyncClient) -> None:
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_error"
