"""RabbitMQ topology: exchanges, work queues, TTL retry queues and dead-letter queues.

Declared idempotently by every publisher/consumer on connect. The same topology is
exported to infra/rabbitmq/definitions.json (regenerate with
`python -m platform_messaging.topology > infra/rabbitmq/definitions.json`); a test keeps
the two in sync.

For a work queue `Q` bound to exchange `E` with routing key `K`:

    E --K--> Q (quorum, delivery-limit) --nack/limit--> E.dlx --> E.dlq
    consumer failure  --> E.retry --Q.retry.10s--> (TTL) --dead-letter--> E --K--> Q
    retries exhausted / permanent failure --> E.dlx --> E.dlq
"""

import json
from dataclasses import dataclass, field
from typing import Any

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange

DEFAULT_RETRY_DELAYS_MS = (10_000, 60_000, 600_000)


def _delay_label(ms: int) -> str:
    if ms % 60_000 == 0:
        return f"{ms // 60_000}m"
    if ms % 1000 == 0:
        return f"{ms // 1000}s"
    return f"{ms}ms"


@dataclass(frozen=True)
class WorkQueue:
    name: str
    exchange: str
    routing_key: str
    retry_delays_ms: tuple[int, ...] = DEFAULT_RETRY_DELAYS_MS
    # Redeliveries without an ack (e.g. the worker crashed) before dead-lettering.
    delivery_limit: int = 5

    @property
    def retry_exchange(self) -> str:
        return f"{self.exchange}.retry"

    @property
    def dead_letter_exchange(self) -> str:
        return f"{self.exchange}.dlx"

    @property
    def dead_letter_queue(self) -> str:
        return f"{self.exchange}.dlq"

    @property
    def retry_queues(self) -> list[tuple[str, int]]:
        return [(f"{self.name}.retry.{_delay_label(ms)}", ms) for ms in self.retry_delays_ms]

    @property
    def max_retries(self) -> int:
        return len(self.retry_delays_ms)

    def queue_arguments(self) -> dict[str, Any]:
        return {
            "x-queue-type": "quorum",
            "x-delivery-limit": self.delivery_limit,
            "x-dead-letter-exchange": self.dead_letter_exchange,
            "x-dead-letter-routing-key": self.dead_letter_queue,
        }

    def retry_queue_arguments(self, ttl_ms: int) -> dict[str, Any]:
        return {
            "x-message-ttl": ttl_ms,
            "x-dead-letter-exchange": self.exchange,
            "x-dead-letter-routing-key": self.routing_key,
        }


def ingest_documents_queue(
    exchange: str = "ingest", retry_delays_ms: tuple[int, ...] = DEFAULT_RETRY_DELAYS_MS
) -> WorkQueue:
    """The document ingestion queue. Services sharing a broker (or tests) can use a
    different exchange name; publisher and worker must agree on both arguments."""
    return WorkQueue(
        name=f"{exchange}.documents",
        exchange=exchange,
        routing_key="documents.ingest",
        retry_delays_ms=retry_delays_ms,
    )


INGEST_DOCUMENTS = ingest_documents_queue()

WORK_QUEUES: tuple[WorkQueue, ...] = (INGEST_DOCUMENTS,)


@dataclass
class DeclaredExchanges:
    main: AbstractExchange
    retry: AbstractExchange
    dead_letter: AbstractExchange
    by_name: dict[str, AbstractExchange] = field(default_factory=dict)


async def declare(channel: AbstractChannel, queue: WorkQueue) -> DeclaredExchanges:
    main = await channel.declare_exchange(queue.exchange, aio_pika.ExchangeType.TOPIC, durable=True)
    retry = await channel.declare_exchange(
        queue.retry_exchange, aio_pika.ExchangeType.DIRECT, durable=True
    )
    dlx = await channel.declare_exchange(
        queue.dead_letter_exchange, aio_pika.ExchangeType.DIRECT, durable=True
    )

    work = await channel.declare_queue(queue.name, durable=True, arguments=queue.queue_arguments())
    await work.bind(main, routing_key=queue.routing_key)

    for name, ttl in queue.retry_queues:
        retry_queue = await channel.declare_queue(
            name, durable=True, arguments=queue.retry_queue_arguments(ttl)
        )
        await retry_queue.bind(retry, routing_key=name)

    dlq = await channel.declare_queue(
        queue.dead_letter_queue, durable=True, arguments={"x-queue-type": "quorum"}
    )
    await dlq.bind(dlx, routing_key=queue.dead_letter_queue)

    exchanges = {e.name: e for e in (main, retry, dlx)}
    return DeclaredExchanges(main=main, retry=retry, dead_letter=dlx, by_name=exchanges)


def definitions(queues: tuple[WorkQueue, ...] = WORK_QUEUES, vhost: str = "/") -> dict[str, Any]:
    """The topology in RabbitMQ definitions-file format (no users or permissions)."""
    exchanges: dict[str, dict[str, Any]] = {}
    out_queues: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []

    def exchange(name: str, kind: str) -> None:
        exchanges[name] = {
            "name": name,
            "vhost": vhost,
            "type": kind,
            "durable": True,
            "auto_delete": False,
            "internal": False,
            "arguments": {},
        }

    def queue(name: str, arguments: dict[str, Any]) -> None:
        out_queues.append(
            {
                "name": name,
                "vhost": vhost,
                "durable": True,
                "auto_delete": False,
                "arguments": arguments,
            }
        )

    def bind(source: str, destination: str, routing_key: str) -> None:
        bindings.append(
            {
                "source": source,
                "vhost": vhost,
                "destination": destination,
                "destination_type": "queue",
                "routing_key": routing_key,
                "arguments": {},
            }
        )

    for q in queues:
        exchange(q.exchange, "topic")
        exchange(q.retry_exchange, "direct")
        exchange(q.dead_letter_exchange, "direct")
        queue(q.name, q.queue_arguments())
        bind(q.exchange, q.name, q.routing_key)
        for name, ttl in q.retry_queues:
            queue(name, q.retry_queue_arguments(ttl))
            bind(q.retry_exchange, name, name)
        if q.dead_letter_queue not in {x["name"] for x in out_queues}:
            queue(q.dead_letter_queue, {"x-queue-type": "quorum"})
            bind(q.dead_letter_exchange, q.dead_letter_queue, q.dead_letter_queue)

    return {"exchanges": list(exchanges.values()), "queues": out_queues, "bindings": bindings}


if __name__ == "__main__":
    print(json.dumps(definitions(), indent=2))
