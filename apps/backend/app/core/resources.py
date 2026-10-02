from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.rate_limit import RateLimiter
from app.db.session import create_engine, create_sessionmaker


@dataclass
class Resources:
    """Process-wide clients, created once at startup and shared by requests."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    rate_limiter: RateLimiter


@asynccontextmanager
async def lifespan_resources(settings: Settings) -> AsyncGenerator[Resources]:
    # Connections are established lazily, so the app starts even if a
    # dependency is down; /readyz reports that instead.
    engine = create_engine(settings)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield Resources(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis,
            rate_limiter=RateLimiter(redis),
        )
    finally:
        await redis.aclose()
        await engine.dispose()


def get_resources(app: FastAPI) -> Resources:
    resources: Resources = app.state.resources
    return resources
