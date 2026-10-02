from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.breaker import CircuitBreaker
from gateway.cache import InMemoryVectorIndex, QdrantVectorIndex, ResponseCache, VectorIndex
from gateway.config import GatewaySettings
from gateway.guardrails import Guardrail, PIIRedactor, PromptInjectionGuard
from gateway.quota import QuotaLookup, QuotaService, db_quota_lookup
from gateway.routing import RouteTable
from gateway.service import GatewayService
from gateway.upstream import LiteLLMClient
from gateway.usage import DbUsageRecorder, UsageRecorder
from platform_core.rate_limit import RateLimiter
from platform_db.session import create_engine, create_sessionmaker

logger = structlog.get_logger(__name__)


@dataclass
class Overrides:
    """Test seams: swap infrastructure without touching the wiring."""

    routes: RouteTable | None = None
    upstream_transport: httpx.AsyncBaseTransport | None = None
    redis: Redis | None = None
    vector_index: VectorIndex | None = None
    usage: UsageRecorder | None = None
    quota_lookup: QuotaLookup | None = None


@dataclass
class Resources:
    settings: GatewaySettings
    engine: AsyncEngine
    redis: Redis
    upstream: LiteLLMClient
    service: GatewayService


def build_guardrails(settings: GatewaySettings) -> list[Guardrail]:
    guardrails: list[Guardrail] = []
    if settings.guardrail_redact_pii:
        guardrails.append(PIIRedactor())
    if settings.guardrail_injection_mode != "off":
        guardrails.append(PromptInjectionGuard(settings.guardrail_injection_mode))
    return guardrails


@asynccontextmanager
async def lifespan_resources(
    settings: GatewaySettings, overrides: Overrides
) -> AsyncGenerator[Resources]:
    routes = overrides.routes or RouteTable.load(settings.routes_file)
    engine = create_engine(settings.database_url, echo=settings.database_echo, pool_size=5)
    sessionmaker = create_sessionmaker(engine)
    redis = overrides.redis or Redis.from_url(settings.redis_url, decode_responses=True)
    upstream = LiteLLMClient(
        settings.litellm_url,
        settings.litellm_master_key.get_secret_value(),
        timeout=settings.upstream_timeout_seconds,
        transport=overrides.upstream_transport,
    )

    index: VectorIndex | None = overrides.vector_index
    qdrant: QdrantVectorIndex | None = None
    if index is None and settings.semantic_cache_enabled:
        try:
            qdrant = QdrantVectorIndex(settings.qdrant_url)
            index = qdrant
        except Exception as exc:  # noqa: BLE001
            logger.warning("qdrant_unavailable_using_memory_index", error=repr(exc))
            index = InMemoryVectorIndex()

    service = GatewayService(
        settings=settings,
        routes=routes,
        upstream=upstream,
        breaker=CircuitBreaker(
            redis,
            threshold=settings.breaker_failure_threshold,
            window_seconds=settings.breaker_window_seconds,
            cooldown_seconds=settings.breaker_cooldown_seconds,
        ),
        rpm_limiter=RateLimiter(redis),
        cache=ResponseCache(
            redis,
            index,
            ttl_seconds=settings.cache_ttl_seconds,
            semantic_threshold=settings.semantic_cache_threshold,
        ),
        quota=QuotaService(
            redis,
            overrides.quota_lookup or db_quota_lookup(sessionmaker),
            default_daily_tokens=settings.default_daily_token_quota,
        ),
        usage=overrides.usage or DbUsageRecorder(sessionmaker),
        guardrails=build_guardrails(settings),
    )
    try:
        yield Resources(
            settings=settings, engine=engine, redis=redis, upstream=upstream, service=service
        )
    finally:
        await upstream.aclose()
        if qdrant is not None:
            await qdrant.aclose()
        if overrides.redis is None:
            await redis.aclose()
        await engine.dispose()
