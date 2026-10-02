"""Integration fixtures: real Postgres + Redis (docker compose locally, services in CI).

Tests are skipped when the services are unreachable locally, but fail in CI.
"""

import argparse
import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy.engine import make_url

from platform_api.core.config import Settings
from platform_api.core.security import hash_password
from platform_api.main import create_app
from platform_api.repositories import tenants as tenants_repo
from platform_api.repositories import users as users_repo
from platform_db.models import UserRole
from platform_db.session import create_engine, create_sessionmaker
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, make_client, make_settings
from tests.fake_gateway import FakeGateway

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[4]  # apps/backend
ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "admin12345"
MEMBER_EMAIL = "member@test.local"
MEMBER_PASSWORD = "member12345"


def _asyncpg_dsn(database: str | None = None) -> str:
    url = make_url(TEST_DATABASE_URL).set(drivername="postgresql")
    if database is not None:
        url = url.set(database=database)
    return url.render_as_string(hide_password=False)


async def _ensure_database() -> None:
    db_name = make_url(TEST_DATABASE_URL).database
    conn = await asyncpg.connect(_asyncpg_dsn("postgres"), timeout=3)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()


async def _services_available() -> str | None:
    try:
        await _ensure_database()
        redis = Redis.from_url(TEST_REDIS_URL)
        try:
            await redis.ping()
        finally:
            await redis.aclose()
    except Exception as exc:  # noqa: BLE001
        return repr(exc)
    return None


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> None:
    error = asyncio.run(_services_available())
    if error:
        if os.getenv("CI"):
            pytest.fail(f"Integration services unavailable in CI: {error}")
        pytest.skip(f"Postgres/Redis not reachable ({error}); run `docker compose up`")

    cfg = Config(str(BACKEND_DIR / "libs" / "db" / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"database_url={TEST_DATABASE_URL}"])
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture(autouse=True)
async def _clean_state(settings: Settings) -> AsyncGenerator[None]:
    """Reset tables and Redis, then seed an admin and a member in one tenant."""
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute("TRUNCATE llm_usage, api_keys, users, tenants CASCADE")
    finally:
        await conn.close()

    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.flushdb()
    finally:
        await redis.aclose()

    engine = create_engine(settings.database_url)
    try:
        async with create_sessionmaker(engine)() as session:
            tenant = await tenants_repo.create(session, name="test-tenant")
            await users_repo.create(
                session,
                tenant_id=tenant.id,
                email=ADMIN_EMAIL,
                password_hash=hash_password(ADMIN_PASSWORD),
                role=UserRole.ADMIN,
            )
            await users_repo.create(
                session,
                tenant_id=tenant.id,
                email=MEMBER_EMAIL,
                password_hash=hash_password(MEMBER_PASSWORD),
            )
            await session.commit()
    finally:
        await engine.dispose()
    yield


@pytest.fixture
def fake_gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
async def client(
    settings: Settings, fake_gateway: FakeGateway
) -> AsyncGenerator[httpx.AsyncClient]:
    app = create_app(settings, gateway_transport=httpx.MockTransport(fake_gateway.handler))
    async for c in make_client(app):
        yield c


async def login(client: httpx.AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


@pytest.fixture
async def admin_headers(client: httpx.AsyncClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)}"}
