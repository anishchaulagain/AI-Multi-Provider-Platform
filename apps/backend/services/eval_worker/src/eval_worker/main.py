import asyncio
import signal
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import structlog
from qdrant_client import AsyncQdrantClient

from eval_worker.config import EvalWorkerSettings, get_settings
from eval_worker.scorer import AnswerScorer
from llm_client import GatewayClient
from platform_core.logging import configure_logging
from platform_db.session import create_engine, create_sessionmaker
from platform_evals import AnswerEvaluator
from platform_messaging import EvaluateAnswerJob, RetryingConsumer
from platform_rag.store import ChunkStore

logger = structlog.get_logger(__name__)


@dataclass
class Worker:
    scorer: AnswerScorer
    consumer: RetryingConsumer[EvaluateAnswerJob]


@asynccontextmanager
async def build_worker(
    settings: EvalWorkerSettings,
    *,
    gateway_transport: httpx.AsyncBaseTransport | None = None,
    qdrant: AsyncQdrantClient | None = None,
) -> AsyncGenerator[Worker]:
    engine = create_engine(settings.database_url, pool_size=2)
    qdrant_client = qdrant or AsyncQdrantClient(url=settings.qdrant_url, timeout=30)
    gateway = GatewayClient(
        settings.gateway_url,
        settings.gateway_service_token.get_secret_value(),
        timeout=settings.gateway_timeout_seconds,
        transport=gateway_transport,
    )
    scorer = AnswerScorer(
        sessionmaker=create_sessionmaker(engine),
        chunks=ChunkStore(qdrant_client, settings.embed_model_name),
        evaluator=AnswerEvaluator(gateway, alias=settings.judge_alias),
    )
    consumer = RetryingConsumer(
        settings.rabbitmq_url,
        settings.eval_queue(),
        EvaluateAnswerJob,
        scorer.handle,
        prefetch=settings.prefetch,
        job_timeout_s=settings.job_timeout_seconds,
    )
    try:
        yield Worker(scorer=scorer, consumer=consumer)
    finally:
        await gateway.aclose()
        if qdrant is None:
            await qdrant_client.close()
        await engine.dispose()


async def run(settings: EvalWorkerSettings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass

    async with build_worker(settings) as worker:
        await worker.consumer.start()
        logger.info("eval_worker_ready", environment=settings.environment)
        try:
            await stop.wait()
        finally:
            logger.info("eval_worker_stopping")
            await worker.consumer.stop(timeout=settings.job_timeout_seconds)


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
