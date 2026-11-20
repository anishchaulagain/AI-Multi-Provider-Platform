"""Context capsules: share a conversation's compact state, view it read-only via a
signed link, and fork it into a new conversation (optionally on another model)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import select

from platform_api.api.deps import CurrentPrincipal, DbSession, ResourcesDep
from platform_api.api.routes.conversations import _check_alias
from platform_api.schemas.conversation import (
    CapsuleCreate,
    CapsuleFork,
    CapsuleRead,
    ConversationRead,
)
from platform_api.services import chat as chat_service
from platform_api.services import history as history_service
from platform_core.errors import NotFoundError
from platform_db.models import ContextCapsule

router = APIRouter(tags=["capsules"])


@router.post(
    "/conversations/{conversation_id}/capsules",
    response_model=CapsuleRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_capsule(
    conversation_id: uuid.UUID,
    body: CapsuleCreate,
    principal: CurrentPrincipal,
    session: DbSession,
    resources: ResourcesDep,
    request: Request,
) -> CapsuleRead:
    """Snapshot the conversation (summary, facts, key Q/A, citation refs) as an
    immutable capsule with a share link. Summarizes pending history first."""
    conversation = await chat_service.get_conversation(
        session,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        conversation_id=conversation_id,
    )
    capsule = await history_service.create_capsule(
        session,
        resources,
        conversation=conversation,
        user_id=principal.user_id,
        title=body.title,
        expires_in_days=body.expires_in_days,
        request_id=getattr(request.state, "request_id", None),
    )
    return CapsuleRead.model_validate(capsule)


@router.get("/capsules", response_model=list[CapsuleRead])
async def list_capsules(
    principal: CurrentPrincipal,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CapsuleRead]:
    rows = await session.execute(
        select(ContextCapsule)
        .where(
            ContextCapsule.tenant_id == principal.tenant_id,
            ContextCapsule.created_by == principal.user_id,
        )
        .order_by(ContextCapsule.created_at.desc())
        .limit(limit)
    )
    return [CapsuleRead.model_validate(c) for c in rows.scalars()]


@router.get("/capsules/{share_token}")
async def view_capsule(
    share_token: str, principal: CurrentPrincipal, session: DbSession, resources: ResourcesDep
) -> dict[str, Any]:
    """Read-only view through a share link: signed token, same workspace, not expired.
    Cited passages are resolved for display (only within the viewer's workspace)."""
    capsule = await history_service.capsule_by_token(
        session, resources, share_token, principal.tenant_id
    )
    refs = capsule.content.get("citations", [])
    chunks = {
        c.id: c
        for c in await resources.chunks.get_chunks(
            principal.tenant_id, [r["chunk_id"] for r in refs]
        )
    }
    citations = []
    for ref in refs:
        chunk = chunks.get(ref["chunk_id"])
        citations.append(
            {
                **ref,
                "missing": chunk is None,
                "doc_title": chunk.doc_title if chunk else None,
                "page": chunk.page if chunk else None,
                "snippet": " ".join(chunk.text.split())[:280] if chunk else None,
            }
        )
    return {
        **CapsuleRead.model_validate(capsule).model_dump(mode="json"),
        "resolved_citations": citations,
    }


@router.post(
    "/capsules/{capsule_id}/fork",
    response_model=ConversationRead,
    status_code=status.HTTP_201_CREATED,
)
async def fork_capsule(
    capsule_id: uuid.UUID,
    body: CapsuleFork,
    principal: CurrentPrincipal,
    session: DbSession,
    resources: ResourcesDep,
) -> ConversationRead:
    """Start a new conversation seeded with the capsule, optionally on another alias."""
    capsule = await history_service.capsule_for_tenant(session, capsule_id, principal.tenant_id)
    conversation = await history_service.fork_capsule(
        session,
        capsule,
        user_id=principal.user_id,
        alias=await _check_alias(resources, body.alias),
        title=body.title,
    )
    return ConversationRead.model_validate(conversation)


@router.delete("/capsules/{capsule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_capsule(
    capsule_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession
) -> None:
    capsule = await session.get(ContextCapsule, capsule_id)
    if (
        capsule is None
        or capsule.tenant_id != principal.tenant_id
        or capsule.created_by != principal.user_id
    ):
        raise NotFoundError("Capsule not found")
    await session.delete(capsule)
    await session.commit()
