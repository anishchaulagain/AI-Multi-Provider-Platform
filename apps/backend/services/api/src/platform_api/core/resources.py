from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from llm_client import GatewayClient
from platform_api.core.config import Settings
from platform_core.rate_limit import RateLimiter
from platform_db.session import create_engine, create_sessionmaker


@dataclass
class Resources:
    """Process-wide clients, created once at startup and shared by requests."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    rate_limiter: RateLimiter
    gateway: GatewayClient


@asynccontextmanager
async def lifespan_resources(
    settings: Settings, *, gateway_transport: httpx.AsyncBaseTransport | None = None
) -> AsyncGenerator[Resources]:
    # Connections are established lazily, so the app starts even if a
    # dependency is down; /readyz reports that instead.
    engine = create_engine(settings.database_url, echo=settings.database_echo)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    gateway = GatewayClient(
        settings.gateway_url,
        settings.gateway_service_token.get_secret_value(),
        timeout=settings.gateway_timeout_seconds,
        transport=gateway_transport,
    )
    try:
        yield Resources(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis,
            rate_limiter=RateLimiter(redis),
            gateway=gateway,
        )
    finally:
        await gateway.aclose()
        await redis.aclose()
        await engine.dispose()


def get_resources(app: FastAPI) -> Resources:
    resources: Resources = app.state.resources
    return resources
