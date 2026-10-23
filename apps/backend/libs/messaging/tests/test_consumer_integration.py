"""Against a real RabbitMQ (skipped when unreachable). Each test uses its own
exchange/queues with millisecond retry delays, deleted afterwards."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable

import aio_pika
import pytest
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractRobustConnection
from pydantic import BaseModel

from platform_messaging import Delivery, PermanentError, Publisher, RetryingConsumer, WorkQueue

RABBITMQ_URL = os.getenv("TEST_RABBITMQ_URL", "amqp://platform:platform@localhost:5672/")

pytestmark = pytest.mark.integration


class Job(BaseModel):
    n: int


Behaviour = Callable[[Job, Delivery], Awaitable[None]]


@pytest.fixture(scope="session")
async def rabbit() -> AsyncGenerator[AbstractRobustConnection]:
    try:
        connection = await aio_pika.connect_robust(RABBITMQ_URL, timeout=3)
    except Exception as exc:  # noqa: BLE001
        if os.getenv("CI"):
            pytest.fail(f"RabbitMQ unavailable in CI: {exc!r}")
        pytest.skip(f"RabbitMQ unavailable: {exc!r}")
    yield connection
    await connection.close()


@pytest.fixture
async def channel(rabbit: AbstractRobustConnection) -> AsyncGenerator[AbstractChannel]:
    ch = await rabbit.channel()
    yield ch
    await ch.close()


@pytest.fixture
async def work_queue(channel: AbstractChannel) -> AsyncGenerator[WorkQueue]:
    tag = uuid.uuid4().hex[:8]
    queue = WorkQueue(
        name=f"test-{tag}.jobs",
        exchange=f"test-{tag}",
        routing_key="jobs.run",
        retry_delays_ms=(100, 200),
        delivery_limit=2,
    )
    yield queue
    for name in [queue.name, *(n for n, _ in queue.retry_queues), queue.dead_letter_queue]:
        await channel.queue_delete(name)
    for name in (queue.exchange, queue.retry_exchange, queue.dead_letter_exchange):
        await channel.exchange_delete(name)


async def _get(
    channel: AbstractChannel, queue: str, timeout: float = 5.0
) -> AbstractIncomingMessage:
    q = await channel.get_queue(queue)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        message = await q.get(no_ack=True, fail=False)
        if message is not None:
            return message
        await asyncio.sleep(0.05)
    raise AssertionError(f"no message on {queue} within {timeout}s")


class Recorder:
    def __init__(self, behaviour: Behaviour) -> None:
        self.behaviour = behaviour
        self.deliveries: list[Delivery] = []
        self.failures: list[tuple[int, float | None, str]] = []
        self.done = asyncio.Event()

    async def handle(self, job: Job, delivery: Delivery) -> None:
        self.deliveries.append(delivery)
        await self.behaviour(job, delivery)
        self.done.set()

    async def on_failure(
        self, job: Job, delivery: Delivery, exc: BaseException, retry: float | None
    ) -> None:
        self.failures.append((delivery.attempt, retry, str(exc)))
        if retry is None:
            self.done.set()


async def _publish(work_queue: WorkQueue, job: Job) -> None:
    publisher = Publisher(RABBITMQ_URL, (work_queue,))
    await publisher.publish(work_queue, job, request_id="req-1")
    await publisher.close()


async def _run(work_queue: WorkQueue, recorder: Recorder, job: Job) -> None:
    consumer = RetryingConsumer(
        RABBITMQ_URL, work_queue, Job, recorder.handle, on_failure=recorder.on_failure
    )
    await consumer.start()
    try:
        await _publish(work_queue, job)
        await asyncio.wait_for(recorder.done.wait(), timeout=10)
    finally:
        await consumer.stop(timeout=5)


async def test_success_is_acked(work_queue: WorkQueue, channel: AbstractChannel) -> None:
    async def ok(job: Job, d: Delivery) -> None:
        assert job.n == 1

    recorder = Recorder(ok)
    await _run(work_queue, recorder, Job(n=1))
    [delivery] = recorder.deliveries
    assert delivery.attempt == 0
    assert delivery.request_id == "req-1"
    assert delivery.traceparent and delivery.traceparent.startswith("00-")
    q = await channel.declare_queue(work_queue.name, passive=True)
    assert q.declaration_result.message_count == 0


async def test_transient_failure_retries_then_succeeds(work_queue: WorkQueue) -> None:
    async def flaky(job: Job, d: Delivery) -> None:
        if d.attempt < 2:
            raise ConnectionError("upstream down")

    recorder = Recorder(flaky)
    await _run(work_queue, recorder, Job(n=1))
    assert [d.attempt for d in recorder.deliveries] == [0, 1, 2]
    assert [(a, r) for a, r, _ in recorder.failures] == [(0, 0.1), (1, 0.2)]


async def test_retries_exhausted_go_to_dlq(work_queue: WorkQueue, channel: AbstractChannel) -> None:
    async def always(job: Job, d: Delivery) -> None:
        raise ConnectionError("still down")

    recorder = Recorder(always)
    await _run(work_queue, recorder, Job(n=7))
    assert len(recorder.deliveries) == 3  # first try + 2 retries
    assert recorder.failures[-1][1] is None

    dead = await _get(channel, work_queue.dead_letter_queue)
    assert json.loads(dead.body) == {"n": 7}
    assert dead.headers["x-error-type"] == "ConnectionError"
    assert "still down" in str(dead.headers["x-error"])
    assert dead.headers["x-original-queue"] == work_queue.name


async def test_permanent_error_skips_retries(
    work_queue: WorkQueue, channel: AbstractChannel
) -> None:
    async def corrupt(job: Job, d: Delivery) -> None:
        raise PermanentError("file is corrupt")

    recorder = Recorder(corrupt)
    await _run(work_queue, recorder, Job(n=3))
    assert len(recorder.deliveries) == 1
    dead = await _get(channel, work_queue.dead_letter_queue)
    assert dead.headers["x-error"] == "file is corrupt"
    assert dead.headers["x-error-type"] == "PermanentError"


async def test_poison_message_is_dead_lettered(
    work_queue: WorkQueue, channel: AbstractChannel
) -> None:
    async def never(job: Job, d: Delivery) -> None:
        raise AssertionError("handler must not run")

    recorder = Recorder(never)
    consumer = RetryingConsumer(RABBITMQ_URL, work_queue, Job, recorder.handle)
    await consumer.start()
    try:
        exchange = await channel.get_exchange(work_queue.exchange)
        await exchange.publish(aio_pika.Message(b"not json"), routing_key=work_queue.routing_key)
        dead = await _get(channel, work_queue.dead_letter_queue)
    finally:
        await consumer.stop(timeout=5)
    assert dead.body == b"not json"
    assert recorder.deliveries == []


async def test_unacked_message_is_redelivered_after_crash(work_queue: WorkQueue) -> None:
    started = asyncio.Event()

    async def hang(job: Job, d: Delivery) -> None:
        started.set()
        await asyncio.sleep(3600)

    crashed = RetryingConsumer(RABBITMQ_URL, work_queue, Job, Recorder(hang).handle)
    await crashed.start()
    await _publish(work_queue, Job(n=5))
    await asyncio.wait_for(started.wait(), timeout=10)
    # Simulate the worker dying: drop the connection without acking.
    assert crashed._connection is not None
    await crashed._connection.close()

    async def ok(job: Job, d: Delivery) -> None:
        return None

    recorder = Recorder(ok)
    survivor = RetryingConsumer(RABBITMQ_URL, work_queue, Job, recorder.handle)
    await survivor.start()
    try:
        await asyncio.wait_for(recorder.done.wait(), timeout=10)
    finally:
        await survivor.stop(timeout=5)
    assert recorder.deliveries[0].redelivered is True
