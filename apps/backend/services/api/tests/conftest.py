import os
from collections.abc import AsyncGenerator

import httpx
import pytest
from fastapi import FastAPI

from platform_api.core.config import Settings

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://platform:platform@localhost:5432/platform_test"
)
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")
TEST_RABBITMQ_URL = os.getenv("TEST_RABBITMQ_URL", "amqp://platform:platform@localhost:5672/")


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "rabbitmq_url": TEST_RABBITMQ_URL,
        "jwt_secret": "test-secret-at-least-32-bytes-long!!",
        "readiness_timeout_seconds": 1.0,
        "seed_admin_password": "admin12345",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


async def make_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    # httpx's ASGITransport doesn't run lifespan, so drive it explicitly.
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
