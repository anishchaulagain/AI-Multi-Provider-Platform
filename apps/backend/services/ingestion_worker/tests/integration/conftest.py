"""End-to-end: Platform API + ingestion worker in one event loop, against real
Postgres, Redis, RabbitMQ, MinIO and Qdrant. Only the AI Gateway is faked.

Isolation: its own database, Redis DB, exchange/queues, bucket and Qdrant collection,
all removed afterwards. Skipped when any service is unreachable.
"""

import argparse
import asyncio
import json
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aio_pika
import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from minio import Minio
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.engine import make_url

from ingestion_worker.config import WorkerSettings
from ingestion_worker.main import Worker, build_worker
from platform_api.core.config import Settings
from platform_api.core.security import hash_password
from platform_api.main import create_app
from platform_api.repositories import tenants as tenants_repo
from platform_api.repositories import users as users_repo
from platform_db.models import UserRole
from platform_db.session import create_engine, create_sessionmaker

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[4]
TAG = f"test-{uuid.uuid4().hex[:8]}"
EMAIL, PASSWORD = "ingest-admin@example.com", "admin12345"

ENV: dict[str, Any] = {
    "database_url": os.getenv(
        "TEST_INGEST_DATABASE_URL",
        "postgresql+asyncpg://platform:platform@localhost:5432/platform_test_ingest",
    ),
    "redis_url": os.getenv("TEST_INGEST_REDIS_URL", "redis://localhost:6379/14"),
    "rabbitmq_url": os.getenv("TEST_RABBITMQ_URL", "amqp://platform:platform@localhost:5672/"),
    "qdrant_url": os.getenv("TEST_QDRANT_URL", "http://localhost:6333"),
    "s3_endpoint": os.getenv("TEST_S3_ENDPOINT", "http://localhost:9000"),
    "s3_access_key": os.getenv("TEST_S3_ACCESS_KEY", "platform"),
    "s3_secret_key": os.getenv("TEST_S3_SECRET_KEY", "platform-minio"),
    "s3_bucket": TAG,
    "ingest_exchange": TAG,
    "ingest_retry_delays_ms": (300, 600),
    "embed_model_name": TAG,
    "environment": "test",
}


def api_settings() -> Settings:
    return Settings(_env_file=None, rate_limit_enabled=False, **ENV)


def worker_settings() -> WorkerSettings:
    return WorkerSettings(_env_file=None, chunk_size=128, chunk_overlap=16, **ENV)


# ---------- fake gateway ----------
@dataclass
class FakeGateway:
    embed_failures: int = 0  # respond 503 to this many embedding calls first
    # Hang the first embedding call forever (simulates a worker dying mid-job).
    hang_first_embed: bool = False
    embed_calls: int = 0
    tenants: set[str] = field(default_factory=set)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.tenants.add(request.headers.get("x-tenant-id", ""))
        if request.url.path == "/v1/embeddings":
            self.embed_calls += 1
            if self.hang_first_embed and self.embed_calls == 1:
                await asyncio.Event().wait()
            if self.embed_failures > 0:
                self.embed_failures -= 1
                return httpx.Response(
                    503, json={"error": {"code": "all_deployments_failed", "message": "down"}}
                )
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": [float(len(t) % 5 + 1), 1.0, float(i % 3)]}
                        for i, t in enumerate(inputs)
                    ]
                },
            )
        reply = json.dumps({"summary": "A test document.", "keywords": ["test", "ingestion"]})
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": reply}}]}
        )


# ---------- infrastructure ----------
def _dsn(url: str, database: str | None = None) -> str:
    parsed = make_url(url).set(drivername="postgresql")
    if database:
        parsed = parsed.set(database=database)
    return parsed.render_as_string(hide_password=False)


async def _reachable() -> str | None:
    checks: dict[str, Callable[[], Awaitable[Any]]] = {
        "postgres": lambda: asyncpg.connect(_dsn(ENV["database_url"], "postgres"), timeout=3),
        "redis": lambda: Redis.from_url(ENV["redis_url"]).ping(),
        "rabbitmq": lambda: aio_pika.connect(ENV["rabbitmq_url"], timeout=3),
        "qdrant": lambda: AsyncQdrantClient(url=ENV["qdrant_url"], timeout=3).get_collections(),
        "minio": lambda: asyncio.to_thread(_minio().list_buckets),
    }
    for name, check in checks.items():
        try:
            result = await asyncio.wait_for(check(), timeout=5)
            if hasattr(result, "close"):
                await result.close()
        except Exception as exc:  # noqa: BLE001
            return f"{name} unavailable: {exc!r}"
    return None


def _minio() -> Minio:
    url = make_url(f"x://{ENV['s3_endpoint'].split('://', 1)[1]}")
    return Minio(
        f"{url.host}:{url.port}",
        access_key=ENV["s3_access_key"],
        secret_key=ENV["s3_secret_key"],
        secure=False,
    )


@pytest.fixture(scope="session")
async def infra() -> AsyncGenerator[None]:
    reason = await _reachable()
    if reason:
        if os.getenv("CI"):
            pytest.fail(f"Integration services unavailable in CI: {reason}")
        pytest.skip(reason)

    database = make_url(ENV["database_url"]).database
    admin = await asyncpg.connect(_dsn(ENV["database_url"], "postgres"))
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database):
            await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    cfg = Config(str(BACKEND_DIR / "libs" / "db" / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"database_url={ENV['database_url']}"])
    await asyncio.to_thread(command.upgrade, cfg, "head")

    yield

    # Teardown: queues/exchanges, collection, bucket.
    connection = await aio_pika.connect(ENV["rabbitmq_url"])
    async with connection:
        channel = await connection.channel()
        for queue in [f"{TAG}.documents", f"{TAG}.dlq"] + [
            f"{TAG}.documents.retry.{s}" for s in ("300ms", "600ms")
        ]:
            await channel.queue_delete(queue)
        for exchange in (TAG, f"{TAG}.retry", f"{TAG}.dlx"):
            await channel.exchange_delete(exchange)
    qdrant = AsyncQdrantClient(url=ENV["qdrant_url"])
    for collection in (await qdrant.get_collections()).collections:
        if collection.name.startswith("chunks_test_"):
            await qdrant.delete_collection(collection.name)
    await qdrant.close()

    def drop_bucket() -> None:
        client = _minio()
        if client.bucket_exists(TAG):
            for obj in client.list_objects(TAG, recursive=True):
                client.remove_object(TAG, obj.object_name or "")
            client.remove_bucket(TAG)

    await asyncio.to_thread(drop_bucket)


@pytest.fixture
async def seeded(infra: None) -> AsyncGenerator[None]:
    conn = await asyncpg.connect(_dsn(ENV["database_url"]))
    try:
        await conn.execute("TRUNCATE documents, llm_usage, api_keys, users, tenants CASCADE")
    finally:
        await conn.close()
    engine = create_engine(ENV["database_url"])
    async with create_sessionmaker(engine)() as session:
        tenant = await tenants_repo.create(session, name="ingest-tenant")
        await users_repo.create(
            session,
            tenant_id=tenant.id,
            email=EMAIL,
            password_hash=hash_password(PASSWORD),
            role=UserRole.ADMIN,
        )
        await session.commit()
    await engine.dispose()
    yield


@dataclass
class Stack:
    client: httpx.AsyncClient
    headers: dict[str, str]
    gateway: FakeGateway
    start_worker: Callable[[], Awaitable[Worker]]

    async def upload(self, name: str, data: bytes) -> httpx.Response:
        return await self.client.post(
            "/api/v1/documents", files={"file": (name, data)}, headers=self.headers
        )

    async def wait_for(
        self, doc_id: str, statuses: set[str], timeout: float = 30
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            doc: dict[str, Any] = (
                await self.client.get(f"/api/v1/documents/{doc_id}", headers=self.headers)
            ).json()
            if doc["status"] in statuses:
                return doc
            if loop.time() > deadline:
                raise AssertionError(f"document stuck in {doc['status']}: {doc}")
            await asyncio.sleep(0.1)


@pytest.fixture
async def stack(seeded: None) -> AsyncIterator[Stack]:
    gateway = FakeGateway()
    app = create_app(api_settings())
    started: list[tuple[Any, Worker]] = []

    async def start_worker() -> Worker:
        cm = build_worker(worker_settings(), gateway_transport=httpx.MockTransport(gateway.handler))
        worker = await cm.__aenter__()
        await worker.consumer.start()
        started.append((cm, worker))
        return worker

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            token = (
                await client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
            ).json()["access_token"]
            try:
                yield Stack(client, {"Authorization": f"Bearer {token}"}, gateway, start_worker)
            finally:
                for cm, worker in reversed(started):
                    try:
                        await worker.consumer.stop(timeout=2)
                    except Exception:  # noqa: BLE001 - already-dropped connections
                        pass
                    await cm.__aexit__(None, None, None)
