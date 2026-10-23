"""Document status events over Redis pub/sub (worker -> API -> browser via SSE).

Events are fire-and-forget notifications; Postgres stays the source of truth, so a
subscriber always reads the current state first and then applies events.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import structlog
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = structlog.get_logger(__name__)


class DocumentEvent(BaseModel):
    doc_id: uuid.UUID
    tenant_id: uuid.UUID
    status: str
    error: str | None = None
    chunk_count: int | None = None
    attempts: int | None = None
    deleted: bool = False
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def tenant_channel(tenant_id: uuid.UUID) -> str:
    return f"docs:{tenant_id}"


def document_channel(tenant_id: uuid.UUID, doc_id: uuid.UUID) -> str:
    return f"docs:{tenant_id}:{doc_id}"


async def publish_document_event(redis: Redis, event: DocumentEvent) -> None:
    payload = event.model_dump_json()
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.publish(tenant_channel(event.tenant_id), payload)
        pipe.publish(document_channel(event.tenant_id, event.doc_id), payload)
        await pipe.execute()
    except RedisError as exc:
        logger.warning("document_event_publish_failed", error=repr(exc))


async def subscribe(
    redis: Redis, channel: str, *, heartbeat_s: float = 15.0
) -> AsyncGenerator[DocumentEvent | None]:
    """Yield events from `channel`; yields None every `heartbeat_s` of silence.

    Subscribes before the first yield, so callers can read the current state after
    the generator has started without missing updates in between.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        yield None  # subscribed
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=heartbeat_s)
            if message is None:
                yield None
                continue
            data = message.get("data")
            try:
                yield DocumentEvent.model_validate_json(data)
            except ValueError:
                logger.warning("document_event_invalid", channel=channel)
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()  # type: ignore[no-untyped-call]
