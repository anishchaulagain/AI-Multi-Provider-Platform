import hashlib
import re
import uuid
from pathlib import PurePath

import structlog
from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from platform_api.core.resources import Resources
from platform_core.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PayloadTooLargeError,
    ServiceUnavailableError,
    UnsupportedMediaTypeError,
)
from platform_db.models import Document, DocumentStatus
from platform_messaging import IngestDocumentJob
from platform_messaging.events import DocumentEvent, publish_document_event
from platform_rag.formats import CONTENT_TYPES, UnsupportedFormatError, detect_format

logger = structlog.get_logger(__name__)

_READ_CHUNK = 1024 * 1024


def safe_filename(name: str) -> str:
    base = PurePath(name.replace("\\", "/")).name or "upload"
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = re.sub(r"[^\w.\- ]+", "_", stem).strip(" .") or "upload"
    ext = re.sub(r"[^\w]+", "", ext)[:10]
    return f"{stem[:200]}.{ext}" if ext else stem[:200]


async def read_limited(file: UploadFile, max_bytes: int) -> bytes:
    data = bytearray()
    while chunk := await file.read(_READ_CHUNK):
        data.extend(chunk)
        if len(data) > max_bytes:
            raise PayloadTooLargeError(
                f"File exceeds the {max_bytes // (1024 * 1024)} MB upload limit",
                details={"max_bytes": max_bytes},
            )
    return bytes(data)


async def get_document(session: AsyncSession, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> Document:
    doc = (
        await session.execute(
            select(Document).where(Document.id == doc_id, Document.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise NotFoundError("Document not found")
    return doc


async def list_documents(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Document], int]:
    where = [Document.tenant_id == tenant_id]
    if status:
        where.append(Document.status == status)
    total = (
        await session.execute(select(func.count()).select_from(Document).where(*where))
    ).scalar_one()
    rows = (
        await session.execute(
            select(Document)
            .where(*where)
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return list(rows), total


async def _publish_status(resources: Resources, doc: Document, *, deleted: bool = False) -> None:
    await publish_document_event(
        resources.redis,
        DocumentEvent(
            doc_id=doc.id,
            tenant_id=doc.tenant_id,
            status=doc.status,
            error=doc.error,
            chunk_count=doc.chunk_count,
            attempts=doc.attempts,
            deleted=deleted,
        ),
    )


async def enqueue(
    session: AsyncSession,
    resources: Resources,
    doc: Document,
    *,
    request_id: str | None,
    traceparent: str | None,
) -> None:
    job = IngestDocumentJob(
        doc_id=doc.id,
        tenant_id=doc.tenant_id,
        storage_key=doc.storage_key,
        filename=doc.filename,
        content_type=doc.content_type,
        content_hash=doc.content_hash,
    )
    try:
        await resources.publisher.publish(
            resources.settings.ingest_queue(), job, request_id=request_id, traceparent=traceparent
        )
    except Exception as exc:
        logger.error("enqueue_failed", doc_id=str(doc.id), error=repr(exc))
        doc.status = DocumentStatus.FAILED
        doc.error = "Could not queue the document for processing. Retry with re-ingest."
        await _commit(session, doc)
        await _publish_status(resources, doc)
        raise ServiceUnavailableError("Ingestion queue is unavailable; please retry") from exc
    await _publish_status(resources, doc)


async def upload(
    session: AsyncSession,
    resources: Resources,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    file: UploadFile,
    request_id: str | None,
    traceparent: str | None,
) -> tuple[Document, bool]:
    """Store the file and queue it. Returns (document, created); an identical file
    already uploaded by this tenant returns the existing document instead."""
    settings = resources.settings
    filename = safe_filename(file.filename or "upload")
    data = await read_limited(file, settings.max_upload_mb * 1024 * 1024)
    if not data:
        raise InvalidRequestError("File is empty")
    try:
        fmt = detect_format(filename, data)
    except UnsupportedFormatError as exc:
        raise UnsupportedMediaTypeError(str(exc)) from exc

    content_hash = hashlib.sha256(data).hexdigest()
    existing = await _by_hash(session, tenant_id, content_hash)
    if existing is not None:
        return existing, False

    doc_id = uuid7()
    doc = Document(
        id=doc_id,
        tenant_id=tenant_id,
        uploaded_by=user_id,
        filename=filename,
        content_type=CONTENT_TYPES[fmt],
        size_bytes=len(data),
        content_hash=content_hash,
        storage_key=f"{tenant_id}/{doc_id}/{filename}",
        status=DocumentStatus.QUEUED,
        attempts=0,
        chunk_count=0,
        details={"format": fmt},
    )
    try:
        await resources.blobs.put(doc.storage_key, data, doc.content_type)
    except Exception as exc:
        logger.error("blob_put_failed", error=repr(exc))
        raise ServiceUnavailableError("File storage is unavailable; please retry") from exc

    session.add(doc)
    try:
        await _commit(session, doc)
    except IntegrityError:
        # Same file uploaded concurrently: keep the winner, drop our copy.
        await session.rollback()
        await resources.blobs.delete_prefix(f"{tenant_id}/{doc_id}/")
        winner = await _by_hash(session, tenant_id, content_hash)
        if winner is None:
            raise
        return winner, False

    await enqueue(session, resources, doc, request_id=request_id, traceparent=traceparent)
    return doc, True


async def _commit(session: AsyncSession, doc: Document) -> None:
    """Commit and reload: server-side columns (timestamps) expire on commit, and
    lazy-loading them later outside the session's greenlet would fail."""
    await session.commit()
    await session.refresh(doc)


async def _by_hash(
    session: AsyncSession, tenant_id: uuid.UUID, content_hash: str
) -> Document | None:
    return (
        await session.execute(
            select(Document).where(
                Document.tenant_id == tenant_id, Document.content_hash == content_hash
            )
        )
    ).scalar_one_or_none()


async def reingest(
    session: AsyncSession,
    resources: Resources,
    doc: Document,
    *,
    request_id: str | None,
    traceparent: str | None,
) -> Document:
    if doc.status not in (DocumentStatus.READY, DocumentStatus.FAILED):
        raise ConflictError(f"Document is already being processed (status: {doc.status})")
    doc.status = DocumentStatus.QUEUED
    doc.error = None
    doc.attempts = 0
    await _commit(session, doc)
    await enqueue(session, resources, doc, request_id=request_id, traceparent=traceparent)
    return doc


async def delete(session: AsyncSession, resources: Resources, doc: Document) -> None:
    """Remove the row first (a running job then stops and cleans up after itself),
    then the stored chunks and the raw file."""
    tenant_id, doc_id = doc.tenant_id, doc.id
    await session.delete(doc)
    await session.commit()
    errors = []
    try:
        await resources.chunks.delete_document(tenant_id, doc_id)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"chunks: {exc!r}")
    try:
        await resources.blobs.delete_prefix(f"{tenant_id}/{doc_id}/")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"file: {exc!r}")
    if errors:
        logger.error("document_cleanup_incomplete", doc_id=str(doc_id), errors=errors)
    await _publish_status(resources, doc, deleted=True)
