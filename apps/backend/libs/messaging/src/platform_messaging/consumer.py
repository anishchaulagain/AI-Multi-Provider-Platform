"""Work-queue consumer with manual acks, TTL-queue retries and a dead-letter queue.

Outcomes per message:
  handler returns          -> ack
  PermanentError           -> dead-letter (with error headers), ack
  any other exception      -> republish to the next retry queue (10s, 1m, 10m), ack;
                              once retries are exhausted, dead-letter
  body fails validation    -> reject(requeue=False): the queue dead-letters it
  worker dies mid-message  -> unacked, so RabbitMQ redelivers it; after the queue's
                              delivery-limit it is dead-lettered
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aio_pika
import structlog
from aio_pika.abc import (
    AbstractChannel,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)
from pydantic import BaseModel, ValidationError

from platform_messaging import topology, tracing

logger = structlog.get_logger(__name__)

ATTEMPT_HEADER = "x-attempt"
ERROR_HEADER = "x-error"
ERROR_TYPE_HEADER = "x-error-type"


class PermanentError(Exception):
    """Retrying won't help (corrupt input, unsupported format...): dead-letter now."""


@dataclass(frozen=True)
class Delivery:
    attempt: int  # 0 on the first try
    max_retries: int
    redelivered: bool
    request_id: str | None
    traceparent: str | None

    @property
    def is_last_attempt(self) -> bool:
        return self.attempt >= self.max_retries


type Handler[M] = Callable[[M, Delivery], Awaitable[None]]
# retry_in_s is None when the message was dead-lettered.
type FailureHook[M] = Callable[[M, Delivery, BaseException, float | None], Awaitable[None]]


def _truncate(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


class RetryingConsumer[M: BaseModel]:
    def __init__(
        self,
        url: str,
        queue: topology.WorkQueue,
        model: type[M],
        handler: Handler[M],
        *,
        on_failure: FailureHook[M] | None = None,
        prefetch: int = 1,
        job_timeout_s: float | None = None,
    ) -> None:
        self._url = url
        self._queue_spec = queue
        self._model = model
        self._handler = handler
        self._on_failure = on_failure
        self._prefetch = prefetch
        self._job_timeout = job_timeout_s
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._queue: AbstractQueue | None = None
        self._exchanges: topology.DeclaredExchanges | None = None
        self._consumer_tag: str | None = None
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def start(self) -> None:
        self._connection = await aio_pika.connect_robust(self._url, timeout=10)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=self._prefetch)
        self._exchanges = await topology.declare(self._channel, self._queue_spec)
        self._queue = await self._channel.get_queue(self._queue_spec.name)
        self._consumer_tag = await self._queue.consume(self._on_message)
        logger.info("consumer_started", queue=self._queue_spec.name, prefetch=self._prefetch)

    async def stop(self, timeout: float = 60.0) -> None:
        """Stop taking new messages, let in-flight ones finish, then disconnect.

        Broker calls are bounded: on a lost connection a robust channel would
        otherwise wait for a reconnect indefinitely."""
        connected = self._connection is not None and not self._connection.is_closed
        if connected and self._queue is not None and self._consumer_tag is not None:
            try:
                await asyncio.wait_for(self._queue.cancel(self._consumer_tag), timeout=5)
            except Exception as exc:  # noqa: BLE001
                logger.warning("consumer_cancel_failed", error=repr(exc))
        self._consumer_tag = None
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning("consumer_stop_timeout", inflight=self._inflight)
        if connected and self._connection is not None:
            try:
                await asyncio.wait_for(self._connection.close(), timeout=5)
            except Exception as exc:  # noqa: BLE001
                logger.warning("consumer_close_failed", error=repr(exc))
        logger.info("consumer_stopped", queue=self._queue_spec.name)

    # ---------- message handling ----------
    async def _on_message(self, message: AbstractIncomingMessage) -> None:
        self._inflight += 1
        self._idle.clear()
        try:
            await self._process(message)
        finally:
            self._inflight -= 1
            if self._inflight == 0:
                self._idle.set()

    async def _process(self, message: AbstractIncomingMessage) -> None:
        headers = dict(message.headers or {})
        traceparent = _str(headers.get(tracing.TRACEPARENT))
        request_id = _str(headers.get(tracing.REQUEST_ID))
        delivery = Delivery(
            attempt=_int(headers.get(ATTEMPT_HEADER)),
            max_retries=self._queue_spec.max_retries,
            redelivered=bool(message.redelivered),
            request_id=request_id,
            traceparent=traceparent,
        )
        log = logger.bind(
            queue=self._queue_spec.name,
            attempt=delivery.attempt,
            request_id=request_id,
            trace_id=tracing.trace_id(traceparent),
            message_id=message.message_id,
        )

        try:
            job = self._model.model_validate_json(message.body)
        except ValidationError as exc:
            log.error("message_invalid", error=_truncate(str(exc), 300))
            await message.reject(requeue=False)
            return

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                coro = self._handler(job, delivery)
                await (asyncio.wait_for(coro, self._job_timeout) if self._job_timeout else coro)
            except asyncio.CancelledError:
                # Shutdown mid-job: leave it unacked so it is redelivered.
                raise
            except PermanentError as exc:
                log.warning("job_failed_permanently", error=str(exc))
                await self._dead_letter(message, headers, delivery, exc)
                await self._notify(job, delivery, exc, None)
            except Exception as exc:  # noqa: BLE001 - every other failure is retried
                if delivery.is_last_attempt:
                    log.error("job_retries_exhausted", error=repr(exc))
                    await self._dead_letter(message, headers, delivery, exc)
                    await self._notify(job, delivery, exc, None)
                else:
                    retry_queue, ttl_ms = self._queue_spec.retry_queues[delivery.attempt]
                    log.warning("job_retry_scheduled", error=repr(exc), retry_in_s=ttl_ms / 1000)
                    await self._republish(
                        message,
                        {
                            **headers,
                            ATTEMPT_HEADER: delivery.attempt + 1,
                            ERROR_HEADER: _truncate(repr(exc)),
                        },
                        self._exchanges_or_raise().retry,
                        retry_queue,
                    )
                    await self._notify(job, delivery, exc, ttl_ms / 1000)
            else:
                log.info("job_done")
            await message.ack()

    async def _dead_letter(
        self,
        message: AbstractIncomingMessage,
        headers: dict[str, Any],
        delivery: Delivery,
        exc: BaseException,
    ) -> None:
        await self._republish(
            message,
            {
                **headers,
                ATTEMPT_HEADER: delivery.attempt,
                ERROR_HEADER: _truncate(str(exc) or repr(exc)),
                ERROR_TYPE_HEADER: type(exc).__name__,
                "x-original-queue": self._queue_spec.name,
                "x-failed-at": datetime.now(UTC).isoformat(),
            },
            self._exchanges_or_raise().dead_letter,
            self._queue_spec.dead_letter_queue,
        )

    @staticmethod
    async def _republish(
        message: AbstractIncomingMessage,
        headers: dict[str, Any],
        exchange: aio_pika.abc.AbstractExchange,
        routing_key: str,
    ) -> None:
        await exchange.publish(
            aio_pika.Message(
                message.body,
                content_type=message.content_type,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                correlation_id=message.correlation_id,
                message_id=message.message_id,
                headers=headers,
            ),
            routing_key=routing_key,
        )

    async def _notify(
        self, job: M, delivery: Delivery, exc: BaseException, retry_in_s: float | None
    ) -> None:
        if self._on_failure is None:
            return
        try:
            await self._on_failure(job, delivery, exc, retry_in_s)
        except Exception as hook_exc:  # noqa: BLE001
            logger.warning("failure_hook_failed", error=repr(hook_exc))

    def _exchanges_or_raise(self) -> topology.DeclaredExchanges:
        if self._exchanges is None:
            raise RuntimeError("consumer not started")
        return self._exchanges


def _int(value: object) -> int:
    try:
        return int(value) if isinstance(value, int | str | bytes) else 0
    except ValueError:
        return 0


def _str(value: object) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)
