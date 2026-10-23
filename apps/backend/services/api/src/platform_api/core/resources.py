import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import structlog
from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from llm_client import GatewayClient
from platform_api.core.config import Settings
from platform_core.rate_limit import RateLimiter
from platform_core.storage import BlobStore, MinioBlobStore
from platform_db.session import create_engine, create_sessionmaker
from platform_graph import GraphStore
from platform_messaging import Publisher
from platform_rag.rerank import Reranker, build_reranker
from platform_rag.retrieve import RetrievalConfig, Retriever
from platform_rag.store import ChunkStore

logger = structlog.get_logger(__name__)


@dataclass
class Overrides:
    """Test seams: swap infrastructure clients without touching the wiring."""

    gateway_transport: httpx.AsyncBaseTransport | None = None
    blobs: BlobStore | None = None
    qdrant: AsyncQdrantClient | None = None
    graph: GraphStore | None = None
    reranker: Reranker | None = None


@dataclass
class Resources:
    """Process-wide clients, created once at startup and shared by requests."""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    rate_limiter: RateLimiter
    gateway: GatewayClient
    blobs: BlobStore
    publisher: Publisher
    chunks: ChunkStore
    graph: GraphStore | None
    retriever: Retriever


@asynccontextmanager
async def lifespan_resources(
    settings: Settings, overrides: Overrides | None = None
) -> AsyncGenerator[Resources]:
    overrides = overrides or Overrides()
    # Connections are established lazily, so the app starts even if a
    # dependency is down; /readyz reports that instead.
    engine = create_engine(settings.database_url, echo=settings.database_echo)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    gateway = GatewayClient(
        settings.gateway_url,
        settings.gateway_service_token.get_secret_value(),
        timeout=settings.gateway_timeout_seconds,
        transport=overrides.gateway_transport,
    )
    blobs = overrides.blobs or MinioBlobStore(
        settings.s3_endpoint,
        settings.s3_access_key,
        settings.s3_secret_key.get_secret_value(),
        settings.s3_bucket,
    )
    qdrant = overrides.qdrant or AsyncQdrantClient(url=settings.qdrant_url, timeout=10)
    publisher = Publisher(settings.rabbitmq_url, (settings.ingest_queue(),))
    chunks = ChunkStore(qdrant, settings.embed_model_name)

    graph = overrides.graph
    if graph is None and settings.graph_enabled:
        graph = GraphStore.connect(
            settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password.get_secret_value()
        )
        try:
            await asyncio.wait_for(graph.ensure_schema(), timeout=10)
        except Exception as exc:  # noqa: BLE001 - retrieval degrades to vectors-only
            logger.warning("graph_schema_init_failed", error=repr(exc))

    reranker = overrides.reranker or build_reranker(settings.reranker, settings.reranker_model)
    retriever = Retriever(
        gateway,
        chunks,
        graph=graph,
        reranker=reranker,
        config=RetrievalConfig(
            embed_alias=settings.embed_alias,
            query_prefix=settings.embed_query_prefix,
            rewrite_alias=settings.rewrite_alias,
            candidates=settings.retrieval_candidates,
            final_k=settings.retrieval_final_k,
            graph_hops=settings.retrieval_graph_hops,
            context_token_budget=settings.retrieval_context_tokens,
        ),
    )
    warm_up = getattr(reranker, "warm_up", None)
    warm_task = asyncio.create_task(_warm_up(warm_up)) if warm_up is not None else None

    try:
        await asyncio.wait_for(blobs.ensure_bucket(), timeout=5)
    except Exception as exc:  # noqa: BLE001 - storage may come up later
        logger.warning("blob_bucket_check_failed", error=repr(exc))

    try:
        yield Resources(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis,
            rate_limiter=RateLimiter(redis),
            gateway=gateway,
            blobs=blobs,
            publisher=publisher,
            chunks=chunks,
            graph=graph,
            retriever=retriever,
        )
    finally:
        if warm_task is not None:
            warm_task.cancel()
        if graph is not None and overrides.graph is None:
            await graph.close()
        await publisher.close()
        await gateway.aclose()
        if overrides.qdrant is None:
            await qdrant.close()
        await redis.aclose()
        await engine.dispose()


async def _warm_up(load: Callable[[], Awaitable[None]]) -> None:
    """Load the reranker model in the background so the first chat isn't slow."""
    try:
        await load()
    except Exception as exc:  # noqa: BLE001 - reranking falls back to fused order
        logger.warning("reranker_warm_up_failed", error=repr(exc))


def get_resources(app: FastAPI) -> Resources:
    resources: Resources = app.state.resources
    return resources
