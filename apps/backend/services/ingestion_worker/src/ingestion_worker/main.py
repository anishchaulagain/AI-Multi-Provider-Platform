import asyncio
import signal
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import structlog
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from ingestion_worker.config import WorkerSettings, get_settings
from ingestion_worker.processor import DocumentProcessor
from llm_client import GatewayClient
from platform_core.logging import configure_logging
from platform_core.storage import BlobStore, MinioBlobStore
from platform_db.session import create_engine, create_sessionmaker
from platform_messaging import IngestDocumentJob, RetryingConsumer
from platform_rag.ingest import IngestionPipeline, PipelineConfig
from platform_rag.store import ChunkStore

logger = structlog.get_logger(__name__)


@dataclass
class Worker:
    processor: DocumentProcessor
    consumer: RetryingConsumer[IngestDocumentJob]


@asynccontextmanager
async def build_worker(
    settings: WorkerSettings,
    *,
    gateway_transport: httpx.AsyncBaseTransport | None = None,
    blobs: BlobStore | None = None,
    qdrant: AsyncQdrantClient | None = None,
) -> AsyncGenerator[Worker]:
    engine = create_engine(settings.database_url, pool_size=2)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    qdrant_client = qdrant or AsyncQdrantClient(url=settings.qdrant_url, timeout=30)
    gateway = GatewayClient(
        settings.gateway_url,
        settings.gateway_service_token.get_secret_value(),
        timeout=settings.gateway_timeout_seconds,
        transport=gateway_transport,
    )
    store = ChunkStore(qdrant_client, settings.embed_model_name)
    processor = DocumentProcessor(
        sessionmaker=create_sessionmaker(engine),
        redis=redis,
        blobs=blobs
        or MinioBlobStore(
            settings.s3_endpoint,
            settings.s3_access_key,
            settings.s3_secret_key.get_secret_value(),
            settings.s3_bucket,
        ),
        store=store,
        pipeline=IngestionPipeline(
            gateway,
            store,
            config=PipelineConfig(
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
                embed_alias=settings.embed_alias,
                embed_batch_size=settings.embed_batch_size,
                embed_document_prefix=settings.embed_document_prefix,
                extract_metadata=settings.extract_metadata,
                extract_alias=settings.extract_alias,
                max_chunks=settings.max_chunks,
            ),
        ),
    )
    consumer = RetryingConsumer(
        settings.rabbitmq_url,
        settings.ingest_queue(),
        IngestDocumentJob,
        processor.handle,
        on_failure=processor.on_failure,
        prefetch=settings.prefetch,
        job_timeout_s=settings.job_timeout_seconds,
    )
    try:
        yield Worker(processor=processor, consumer=consumer)
    finally:
        await gateway.aclose()
        if qdrant is None:
            await qdrant_client.close()
        await redis.aclose()
        await engine.dispose()


async def run(settings: WorkerSettings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass

    async with build_worker(settings) as worker:
        await worker.consumer.start()
        logger.info("worker_ready", environment=settings.environment)
        try:
            await stop.wait()
        finally:
            logger.info("worker_stopping")
            await worker.consumer.stop(timeout=settings.job_timeout_seconds)


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
