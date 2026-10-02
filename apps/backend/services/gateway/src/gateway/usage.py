import uuid
from dataclasses import asdict, dataclass
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from platform_db.models import LlmUsage

logger = structlog.get_logger(__name__)


@dataclass
class UsageEvent:
    tenant_id: str
    user_id: str | None
    request_id: str | None
    kind: str
    alias: str
    deployment: str | None
    provider: str | None
    status: str  # ok | error | cache_hit | blocked
    cache: str  # miss | exact | semantic | off
    fallbacks: int
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class UsageRecorder(Protocol):
    async def record(self, event: UsageEvent) -> None: ...


class InMemoryUsageRecorder:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)


class DbUsageRecorder:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def record(self, event: UsageEvent) -> None:
        try:
            async with self._sessionmaker() as session:
                data = asdict(event)
                session.add(
                    LlmUsage(
                        **{
                            **data,
                            "tenant_id": uuid.UUID(event.tenant_id),
                            "user_id": uuid.UUID(event.user_id) if event.user_id else None,
                            "error": (event.error or None) and event.error[:500],
                            "total_tokens": event.total_tokens,
                        }
                    )
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - usage logging must never fail a request
            logger.warning("usage_record_failed", error=repr(exc))
