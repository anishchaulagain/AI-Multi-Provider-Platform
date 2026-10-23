import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, File, Query, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from platform_api.api.deps import CurrentPrincipal, DbSession, ResourcesDep
from platform_api.core.resources import Resources
from platform_api.schemas.document import ChunkPage, ChunkRead, DocumentList, DocumentRead
from platform_api.services import documents as documents_service
from platform_db.models import DocumentStatus
from platform_messaging import events
from platform_messaging.tracing import TRACEPARENT

router = APIRouter(prefix="/documents", tags=["documents"])

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _trace(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), request.headers.get(TRACEPARENT)


@router.post(
    "",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": DocumentRead, "description": "Identical file already uploaded"}},
)
async def upload_document(
    principal: CurrentPrincipal,
    session: DbSession,
    resources: ResourcesDep,
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File(description="PDF, DOCX, Markdown, HTML or TXT")],
) -> DocumentRead:
    request_id, traceparent = _trace(request)
    doc, created = await documents_service.upload(
        session,
        resources,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        file=file,
        request_id=request_id,
        traceparent=traceparent,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
        response.headers["X-Duplicate"] = "true"
    return DocumentRead.model_validate(doc)


@router.get("", response_model=DocumentList)
async def list_documents(
    principal: CurrentPrincipal,
    session: DbSession,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentList:
    rows, total = await documents_service.list_documents(
        session, principal.tenant_id, status=status_filter, limit=limit, offset=offset
    )
    return DocumentList(items=[DocumentRead.model_validate(d) for d in rows], total=total)


# Declared before /{doc_id} so "events" isn't parsed as a document id.
@router.get("/events", response_class=StreamingResponse)
async def tenant_document_events(
    principal: CurrentPrincipal, resources: ResourcesDep, request: Request
) -> StreamingResponse:
    """SSE stream of status changes for every document of the caller's tenant."""
    channel = events.tenant_channel(principal.tenant_id)
    return StreamingResponse(
        _stream(resources, request, channel), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.get("/{doc_id}", response_model=DocumentRead)
async def get_document(
    doc_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession
) -> DocumentRead:
    return DocumentRead.model_validate(
        await documents_service.get_document(session, principal.tenant_id, doc_id)
    )


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    doc_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession, resources: ResourcesDep
) -> None:
    doc = await documents_service.get_document(session, principal.tenant_id, doc_id)
    await documents_service.delete(session, resources, doc)


@router.post(
    "/{doc_id}/reingest", response_model=DocumentRead, status_code=status.HTTP_202_ACCEPTED
)
async def reingest_document(
    doc_id: uuid.UUID,
    principal: CurrentPrincipal,
    session: DbSession,
    resources: ResourcesDep,
    request: Request,
) -> DocumentRead:
    request_id, traceparent = _trace(request)
    doc = await documents_service.get_document(session, principal.tenant_id, doc_id)
    doc = await documents_service.reingest(
        session, resources, doc, request_id=request_id, traceparent=traceparent
    )
    return DocumentRead.model_validate(doc)


@router.get("/{doc_id}/chunks", response_model=ChunkPage)
async def list_chunks(
    doc_id: uuid.UUID,
    principal: CurrentPrincipal,
    session: DbSession,
    resources: ResourcesDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ChunkPage:
    doc = await documents_service.get_document(session, principal.tenant_id, doc_id)
    chunks = await resources.chunks.list_chunks(
        principal.tenant_id, doc_id, offset=offset, limit=limit
    )
    return ChunkPage(
        items=[ChunkRead(**c.model_dump(exclude={"metadata"})) for c in chunks],
        offset=offset,
        limit=limit,
        total=doc.chunk_count,
    )


@router.get("/{doc_id}/events", response_class=StreamingResponse)
async def document_events(
    doc_id: uuid.UUID,
    principal: CurrentPrincipal,
    session: DbSession,
    resources: ResourcesDep,
    request: Request,
) -> StreamingResponse:
    """SSE stream of one document's status: the current state first, then every change,
    closing once it reaches `ready` or `failed` (or is deleted)."""
    await documents_service.get_document(session, principal.tenant_id, doc_id)
    channel = events.document_channel(principal.tenant_id, doc_id)
    return StreamingResponse(
        _stream(resources, request, channel, snapshot=(principal.tenant_id, doc_id)),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def _sse(event: events.DocumentEvent) -> bytes:
    return f"event: document\ndata: {event.model_dump_json()}\n\n".encode()


async def _stream(
    resources: Resources,
    request: Request,
    channel: str,
    *,
    snapshot: tuple[uuid.UUID, uuid.UUID] | None = None,
) -> AsyncIterator[bytes]:
    subscription = events.subscribe(resources.redis, channel)
    try:
        await anext(subscription)  # subscribed: nothing published from here on is missed
        if snapshot is not None:
            tenant_id, doc_id = snapshot
            # Fresh session: request-scoped dependencies may be closed while streaming.
            async with resources.sessionmaker() as session:
                try:
                    doc = await documents_service.get_document(session, tenant_id, doc_id)
                except Exception:  # deleted in the meantime
                    yield _sse(
                        events.DocumentEvent(
                            doc_id=doc_id, tenant_id=tenant_id, status="deleted", deleted=True
                        )
                    )
                    return
                current = events.DocumentEvent(
                    doc_id=doc.id,
                    tenant_id=doc.tenant_id,
                    status=doc.status,
                    error=doc.error,
                    chunk_count=doc.chunk_count,
                    attempts=doc.attempts,
                )
            yield _sse(current)
            if DocumentStatus(current.status).is_terminal:
                return

        async for event in subscription:
            if await request.is_disconnected():
                return
            if event is None:
                yield b": ping\n\n"
                continue
            yield _sse(event)
            if snapshot is not None and (
                event.deleted or event.status in (DocumentStatus.READY, DocumentStatus.FAILED)
            ):
                return
    finally:
        await subscription.aclose()
