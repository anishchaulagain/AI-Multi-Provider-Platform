import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from platform_api.api.deps import CurrentPrincipal, DbSession, ResourcesDep
from platform_api.schemas.conversation import (
    ChatRequest,
    ConversationCreate,
    ConversationRead,
    ConversationUpdate,
    MessageRead,
)
from platform_api.services import chat as chat_service
from platform_core.errors import InvalidRequestError
from platform_db.models import Conversation, Document

router = APIRouter(prefix="/conversations", tags=["conversations"])


async def _check_alias(resources: ResourcesDep, alias: str | None) -> str | None:
    if alias is None:
        return None
    aliases = await resources.windows.chat_aliases()
    if aliases and alias not in aliases:
        raise InvalidRequestError(f"Unknown chat alias '{alias}'", details={"aliases": aliases})
    return alias


async def _check_documents(
    session: DbSession, tenant_id: uuid.UUID, doc_ids: list[uuid.UUID]
) -> list[str]:
    if not doc_ids:
        return []
    found = set(
        (
            await session.execute(
                select(Document.id).where(Document.tenant_id == tenant_id, Document.id.in_(doc_ids))
            )
        ).scalars()
    )
    unknown = [str(d) for d in doc_ids if d not in found]
    if unknown:
        raise InvalidRequestError("Unknown document ids", details={"doc_ids": unknown})
    return [str(d) for d in dict.fromkeys(doc_ids)]


@router.post("", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreate,
    principal: CurrentPrincipal,
    session: DbSession,
    resources: ResourcesDep,
) -> ConversationRead:
    conversation = Conversation(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        title=body.title or chat_service.DEFAULT_TITLE,
        doc_ids=await _check_documents(session, principal.tenant_id, body.doc_ids),
        alias=await _check_alias(resources, body.alias),
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return ConversationRead.model_validate(conversation)


@router.get("", response_model=list[ConversationRead])
async def list_conversations(
    principal: CurrentPrincipal,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ConversationRead]:
    rows = (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.tenant_id == principal.tenant_id,
                Conversation.user_id == principal.user_id,
            )
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
    ).scalars()
    return [ConversationRead.model_validate(c) for c in rows]


@router.get("/{conversation_id}", response_model=ConversationRead)
async def get_conversation(
    conversation_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession
) -> ConversationRead:
    return ConversationRead.model_validate(
        await chat_service.get_conversation(
            session,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            conversation_id=conversation_id,
        )
    )


@router.patch("/{conversation_id}", response_model=ConversationRead)
async def update_conversation(
    conversation_id: uuid.UUID,
    body: ConversationUpdate,
    principal: CurrentPrincipal,
    session: DbSession,
    resources: ResourcesDep,
) -> ConversationRead:
    conversation = await chat_service.get_conversation(
        session,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        conversation_id=conversation_id,
    )
    if body.title is not None:
        conversation.title = body.title
    if body.alias is not None:
        # An empty string clears the override.
        conversation.alias = await _check_alias(resources, body.alias) if body.alias else None
    if body.doc_ids is not None:
        conversation.doc_ids = await _check_documents(session, principal.tenant_id, body.doc_ids)
    await session.commit()
    await session.refresh(conversation)
    return ConversationRead.model_validate(conversation)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID,
    principal: CurrentPrincipal,
    session: DbSession,
    resources: ResourcesDep,
) -> None:
    conversation = await chat_service.get_conversation(
        session,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        conversation_id=conversation_id,
    )
    await session.delete(conversation)
    await session.commit()
    # Its recall vectors go too (summaries and memory cascade in Postgres).
    await resources.turns.delete_conversation(principal.tenant_id, conversation_id)


@router.get("/{conversation_id}/messages", response_model=list[MessageRead])
async def list_messages(
    conversation_id: uuid.UUID,
    principal: CurrentPrincipal,
    session: DbSession,
    resources: ResourcesDep,
) -> list[MessageRead]:
    conversation = await chat_service.get_conversation(
        session,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        conversation_id=conversation_id,
    )
    return await chat_service.list_messages(session, resources, conversation)


@router.post("/{conversation_id}/chat", response_class=StreamingResponse)
async def chat(
    conversation_id: uuid.UUID,
    body: ChatRequest,
    principal: CurrentPrincipal,
    session: DbSession,
    resources: ResourcesDep,
    request: Request,
) -> StreamingResponse:
    """Answer from the conversation's documents. Streams SSE events `token`, then
    `citations`, `usage` and `done`. Retrieval and model errors before the first
    token are returned as regular HTTP errors."""
    conversation = await chat_service.get_conversation(
        session,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        conversation_id=conversation_id,
    )
    prepared = await chat_service.prepare_chat(
        session,
        resources,
        conversation=conversation,
        user_id=principal.user_id,
        question=body.message.strip(),
        request_id=getattr(request.state, "request_id", None),
        context_capsule_id=body.context_capsule_id,
    )
    return StreamingResponse(
        chat_service.stream_answer(resources, prepared),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            **prepared.stream.meta.to_headers(),
        },
    )
