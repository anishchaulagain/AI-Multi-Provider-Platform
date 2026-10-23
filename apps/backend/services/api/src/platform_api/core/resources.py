import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import structlog
from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from llm_client import GatewayClient
from platform_api.core.config import Settings
from platform_core.rate_limit import RateLimiter
from platform_core.storage import BlobStore, MinioBlobStore
from platform_db.session import create_engine, create_sessionmaker
from platform_messaging import Publisher
from platform_rag.store import ChunkStore

logger = structlog.get_logger(__name__)


@dataclass
class Overrides:
    """Test seams: swap infrastructure clients without touching the wiring."""

    gateway_transport: httpx.AsyncBaseTransport | None = None
    blobs: BlobStore | None = None
    qdrant: AsyncQdrantClient | None = None


@dataclass
class Resources:
    """Process-wide clients, created once at startup and shared by requests."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    rate_limiter: RateLimiter
    gateway: GatewayClient
    blobs: BlobStore
    publisher: Publisher
    chunks: ChunkStore


@asynccontextmanager
async def lifespan_resources(
    settings: Settings, overrides: Overrides | None = None
) -> AsyncGenerator[Resources]:
    overrides = overrides or Overrides()
    # Connections are established lazily, so the app starts even if a
    # dependency is down; /readyz reports that instead.
    engine = create_engine(settings.database_url, echo=settings.database_echo)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    gateway = GatewayClient(
        settings.gateway_url,
        settings.gateway_service_token.get_secret_value(),
        timeout=settings.gateway_timeout_seconds,
        transport=overrides.gateway_transport,
    )
    blobs = overrides.blobs or MinioBlobStore(
        settings.s3_endpoint,
        settings.s3_access_key,
        settings.s3_secret_key.get_secret_value(),
        settings.s3_bucket,
    )
    qdrant = overrides.qdrant or AsyncQdrantClient(url=settings.qdrant_url, timeout=10)
    publisher = Publisher(settings.rabbitmq_url, (settings.ingest_queue(),))

    try:
        await asyncio.wait_for(blobs.ensure_bucket(), timeout=5)
    except Exception as exc:  # noqa: BLE001 - storage may come up later
        logger.warning("blob_bucket_check_failed", error=repr(exc))

    try:
        yield Resources(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis,
            rate_limiter=RateLimiter(redis),
            gateway=gateway,
            blobs=blobs,
            publisher=publisher,
            chunks=ChunkStore(qdrant, settings.embed_model_name),
        )
    finally:
        await publisher.close()
        await gateway.aclose()
        if overrides.qdrant is None:
            await qdrant.close()
        await redis.aclose()
        await engine.dispose()


def get_resources(app: FastAPI) -> Resources:
    resources: Resources = app.state.resources
    return resources
