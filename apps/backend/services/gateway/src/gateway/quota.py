"""Per-tenant daily token quotas, counted in Redis (UTC day buckets)."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from platform_core.errors import QuotaExceededError
from platform_db.models import Tenant

logger = structlog.get_logger(__name__)

# Returns the tenant's own quota, or None to use the default.
QuotaLookup = Callable[[str], Awaitable[int | None]]


def db_quota_lookup(sessionmaker: async_sessionmaker[AsyncSession]) -> QuotaLookup:
    async def lookup(tenant_id: str) -> int | None:
        async with sessionmaker() as session:
            tenant = await session.get(Tenant, uuid.UUID(tenant_id))
            return tenant.daily_token_quota if tenant else None

    return lookup


class QuotaService:
    def __init__(self, redis: Redis, lookup: QuotaLookup, *, default_daily_tokens: int) -> None:
        self._redis = redis
        self._lookup = lookup
        self._default = default_daily_tokens

    @staticmethod
    def _usage_key(tenant_id: str) -> str:
        return f"quota:used:{tenant_id}:{datetime.now(UTC):%Y%m%d}"

    async def limit(self, tenant_id: str) -> int:
        cache_key = f"quota:limit:{tenant_id}"
        try:
            cached = await self._redis.get(cache_key)
            if cached is not None:
                return int(cached)
        except RedisError:
            pass
        own = await self._lookup(tenant_id)
        value = own if own is not None else self._default
        try:
            await self._redis.set(cache_key, value, ex=60)
        except RedisError:
            pass
        return value

    async def usage(self, tenant_id: str) -> int:
        try:
            return int(await self._redis.get(self._usage_key(tenant_id)) or 0)
        except RedisError:
            return 0

    async def check(self, tenant_id: str) -> None:
        limit = await self.limit(tenant_id)
        if limit <= 0:
            return
        used = await self.usage(tenant_id)
        if used >= limit:
            raise QuotaExceededError(
                "Daily token quota exceeded",
                details={"used": used, "limit": limit, "resets": "00:00 UTC"},
            )

    async def consume(self, tenant_id: str, tokens: int) -> None:
        if tokens <= 0:
            return
        key = self._usage_key(tenant_id)
        try:
            pipe = self._redis.pipeline()
            pipe.incrby(key, tokens)
            pipe.expire(key, 60 * 60 * 48)
            await pipe.execute()
        except RedisError:
            logger.warning("quota_consume_failed", tenant_id=tenant_id, tokens=tokens)
