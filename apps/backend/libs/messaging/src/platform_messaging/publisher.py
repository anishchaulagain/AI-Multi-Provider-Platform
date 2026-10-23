import asyncio
from typing import Any

import aio_pika
import structlog
from aio_pika.abc import AbstractChannel, AbstractRobustConnection
from pydantic import BaseModel

from platform_messaging import topology, tracing

logger = structlog.get_logger(__name__)


class Publisher:
    """Publishes JSON messages with publisher confirms.

    Connects lazily, so services start even when RabbitMQ is down; the first
    publish raises instead.
    """

    def __init__(self, url: str, queues: tuple[topology.WorkQueue, ...] = topology.WORK_QUEUES):
        self._url = url
        self._queues = queues
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._lock = asyncio.Lock()

    async def _ensure_channel(self) -> AbstractChannel:
        async with self._lock:
            if self._channel is None or self._channel.is_closed:
                self._connection = await aio_pika.connect_robust(self._url, timeout=5)
                self._channel = await self._connection.channel(publisher_confirms=True)
                for queue in self._queues:
                    await topology.declare(self._channel, queue)
            return self._channel

    async def publish(
        self,
        queue: topology.WorkQueue,
        body: BaseModel,
        *,
        request_id: str | None = None,
        traceparent: str | None = None,
        headers: dict[str, Any] | None = None,
    ) -> None:
        channel = await self._ensure_channel()
        exchange = await channel.get_exchange(queue.exchange)
        message = aio_pika.Message(
            body.model_dump_json().encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            correlation_id=request_id,
            headers={
                tracing.TRACEPARENT: tracing.child_traceparent(traceparent),
                **({tracing.REQUEST_ID: request_id} if request_id else {}),
                **(headers or {}),
            },
        )
        await exchange.publish(message, routing_key=queue.routing_key)
        logger.info("message_published", queue=queue.name, request_id=request_id)

    async def ping(self) -> None:
        await self._ensure_channel()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
        self._connection = self._channel = None
