"""Handles one IngestDocumentJob: tracks status in Postgres, publishes events,
runs the pipeline and decides which failures are worth retrying."""

from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from llm_client import Caller, GatewayError
from platform_core.storage import BlobNotFoundError, BlobStore
from platform_db.models import Document, DocumentStatus
from platform_graph import GraphStore
from platform_messaging import Delivery, IngestDocumentJob, PermanentError
from platform_messaging.events import DocumentEvent, publish_document_event
from platform_rag.formats import DocumentParseError, UnsupportedFormatError
from platform_rag.ingest import IngestionPipeline
from platform_rag.ingest.pipeline import EmbeddingMismatchError, Stage
from platform_rag.store import ChunkStore

logger = structlog.get_logger(__name__)

# Gateway responses that will not change on retry.
_PERMANENT_GATEWAY_STATUS = {400, 401, 403, 404, 413, 422}


def _is_permanent_gateway_error(exc: GatewayError) -> bool:
    return exc.status_code in _PERMANENT_GATEWAY_STATUS or exc.code == "quota_exceeded"


class DocumentProcessor:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,
        blobs: BlobStore,
        store: ChunkStore,
        graph: GraphStore | None = None,
        pipeline: IngestionPipeline,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._redis = redis
        self._blobs = blobs
        self._store = store
        self._graph = graph
        self._pipeline = pipeline

    # ---------- status ----------
    async def _update(self, job: IngestDocumentJob, **values: Any) -> bool:
        """Update the document row and publish an event. False if it no longer exists."""
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    update(Document)
                    .where(Document.id == job.doc_id, Document.tenant_id == job.tenant_id)
                    .values(**values)
                    .returning(
                        Document.status, Document.error, Document.chunk_count, Document.attempts
                    )
                )
            ).one_or_none()
            await session.commit()
        if row is None:
            return False
        await publish_document_event(
            self._redis,
            DocumentEvent(
                doc_id=job.doc_id,
                tenant_id=job.tenant_id,
                status=row.status,
                error=row.error,
                chunk_count=row.chunk_count,
                attempts=row.attempts,
            ),
        )
        return True

    async def _current(self, job: IngestDocumentJob) -> Document | None:
        async with self._sessionmaker() as session:
            return (
                await session.execute(
                    select(Document).where(
                        Document.id == job.doc_id, Document.tenant_id == job.tenant_id
                    )
                )
            ).scalar_one_or_none()

    # ---------- handler ----------
    async def handle(self, job: IngestDocumentJob, delivery: Delivery) -> None:
        log = logger.bind(doc_id=str(job.doc_id), tenant_id=str(job.tenant_id))
        doc = await self._current(job)
        if doc is None:
            log.info("document_gone_skipping")
            return
        if doc.content_hash != job.content_hash:
            log.info("stale_job_skipping")
            return

        await self._update(job, attempts=delivery.attempt + 1, error=None)

        async def on_stage(stage: Stage) -> None:
            if not await self._update(job, status=DocumentStatus(stage).value):
                raise _DocumentDeletedError

        try:
            data = await self._blobs.get(job.storage_key)
            result = await self._pipeline.run(
                tenant_id=job.tenant_id,
                doc_id=job.doc_id,
                filename=job.filename,
                data=data,
                caller=Caller(tenant_id=job.tenant_id, request_id=delivery.request_id),
                on_stage=on_stage,
            )
        except _DocumentDeletedError:
            log.info("document_deleted_mid_job")
            await self._cleanup(job)
            return
        except BlobNotFoundError as exc:
            raise PermanentError("The uploaded file is missing from storage") from exc
        except (DocumentParseError, UnsupportedFormatError, EmbeddingMismatchError) as exc:
            raise PermanentError(str(exc)) from exc
        except GatewayError as exc:
            if _is_permanent_gateway_error(exc):
                raise PermanentError(f"Embedding failed: {exc.message} ({exc.code})") from exc
            raise

        finished = await self._update(
            job,
            status=DocumentStatus.READY.value,
            error=None,
            title=result.title[:500],
            page_count=result.page_count,
            chunk_count=result.chunk_count,
            details=result.details,
            ingested_at=datetime.now(UTC),
        )
        if not finished:  # deleted between the last stage and now
            await self._cleanup(job)

    async def _cleanup(self, job: IngestDocumentJob) -> None:
        """The document was deleted while we worked on it: remove what we wrote."""
        await self._store.delete_document(job.tenant_id, job.doc_id)
        if self._graph is not None:
            await self._graph.delete_document(job.tenant_id, job.doc_id)

    async def on_failure(
        self,
        job: IngestDocumentJob,
        delivery: Delivery,
        exc: BaseException,
        retry_in_s: float | None,
    ) -> None:
        message = str(exc) or type(exc).__name__
        if retry_in_s is None:
            await self._update(job, status=DocumentStatus.FAILED.value, error=message[:2000])
        else:
            await self._update(
                job,
                status=DocumentStatus.QUEUED.value,
                error=(
                    f"Attempt {delivery.attempt + 1} failed ({message[:500]}); "
                    f"retrying in {retry_in_s:g}s"
                ),
            )


class _DocumentDeletedError(Exception):
    pass
