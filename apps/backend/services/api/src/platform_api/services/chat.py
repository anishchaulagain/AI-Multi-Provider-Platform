"""Conversations and grounded chat: budgeted history + retrieval -> stream a cited
answer -> persist -> maybe queue compaction.

SSE events of POST /conversations/{id}/chat, in order:
    token      {"text": "..."}                                (many)
    citations  {"citations": [...], "invalid": [n, ...]}
    usage      {"prompt_tokens", "completion_tokens", "gateway", "retrieval_ms",
                "history": {"raw_history_tokens", "sent_history_tokens", ...}}
    done       {"message_id", "user_message_id"}
or, if the model stream breaks midway,
    error      {"code", "message"} followed by done (the partial answer is saved).
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from llm_client import Caller, ChatCompletionRequest, GatewayError, GatewayStream
from platform_api.core.resources import Resources
from platform_api.schemas.conversation import CitationRead, MessageRead
from platform_api.services import history as history_service
from platform_core.errors import AppError, NotFoundError
from platform_db.models import Conversation, Message, MessageRole
from platform_rag.answer import SYSTEM_PROMPT, build_messages, resolve_citations
from platform_rag.retrieve import RetrievalResult, Turn, estimate_tokens

logger = structlog.get_logger(__name__)

DEFAULT_TITLE = "New conversation"


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


async def get_conversation(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    conversation = (
        await session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == tenant_id,
                Conversation.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise NotFoundError("Conversation not found")
    return conversation


async def list_messages(
    session: AsyncSession, resources: Resources, conversation: Conversation
) -> list[MessageRead]:
    messages = list(
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at, Message.id)
            )
        ).scalars()
    )
    # Resolve stored chunk references to their current text in one lookup.
    chunk_ids = list({c["chunk_id"] for m in messages for c in m.citations})
    chunks = {c.id: c for c in await resources.chunks.get_chunks(conversation.tenant_id, chunk_ids)}
    out = []
    for message in messages:
        citations = []
        for ref in message.citations:
            chunk = chunks.get(ref["chunk_id"])
            if chunk is None:
                citations.append(CitationRead(**ref, missing=True))
                continue
            snippet = " ".join(chunk.text.split())
            citations.append(
                CitationRead(
                    **ref,
                    doc_title=chunk.doc_title,
                    page=chunk.page,
                    section=chunk.section,
                    snippet=snippet[:280],
                    text=chunk.text,
                )
            )
        out.append(
            MessageRead(
                id=message.id,
                role=message.role,
                content=message.content,
                token_count=message.token_count,
                citations=citations,
                usage=message.usage,
                created_at=message.created_at,
            )
        )
    return out


@dataclass
class _Answer:
    parts: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    error: dict[str, str] | None = None

    @property
    def text(self) -> str:
        return "".join(self.parts)


@dataclass
class PreparedChat:
    """Everything that can fail with a proper HTTP error happens before streaming."""

    conversation_id: uuid.UUID
    tenant_id: uuid.UUID
    user_message_id: uuid.UUID
    retrieval: RetrievalResult
    stream: GatewayStream
    stack: AsyncExitStack
    history: dict[str, Any] = field(default_factory=dict)


async def prepare_chat(
    session: AsyncSession,
    resources: Resources,
    *,
    conversation: Conversation,
    user_id: uuid.UUID,
    question: str,
    request_id: str | None,
    context_capsule_id: uuid.UUID | None = None,
) -> PreparedChat:
    settings = resources.settings
    caller = Caller(tenant_id=conversation.tenant_id, user_id=user_id, request_id=request_id)
    doc_ids = [uuid.UUID(d) for d in conversation.doc_ids] or None

    # A capsule this conversation was forked from, or one attached to this request.
    capsule_text: str | None = None
    capsule_ids = [c for c in (conversation.capsule_id, context_capsule_id) if c]
    texts = []
    for capsule_id in dict.fromkeys(capsule_ids):
        capsule = await history_service.capsule_for_tenant(
            session, capsule_id, conversation.tenant_id
        )
        texts.append(history_service.capsule_text(capsule))
    capsule_text = "\n\n".join(texts) or None

    state = await history_service.load_history(
        session, resources, conversation, capsule_text=capsule_text
    )
    alias = conversation.alias or settings.answer_alias
    window = await resources.windows.window(alias, cap=settings.history_max_context_tokens)
    built = await resources.history.build(
        state,
        question,
        window=window,
        system_tokens=resources.counter.count(SYSTEM_PROMPT),
        recall=history_service.recall_fn(resources, conversation, caller),
    )
    # The query rewrite only needs the latest exchange.
    last_turns = [Turn(role=t.role, content=t.content) for t in state.turns[-2:]]  # type: ignore[arg-type]

    try:
        retrieval = await resources.retriever.retrieve(
            conversation.tenant_id,
            question,
            caller=caller,
            history=last_turns,
            doc_ids=doc_ids,
            context_token_budget=built.rag_tokens,
        )
    except GatewayError as exc:
        raise _app_error(exc) from exc

    stack = AsyncExitStack()
    try:
        stream = await stack.enter_async_context(
            resources.gateway.stream(
                ChatCompletionRequest(
                    model=alias,
                    messages=build_messages(question, retrieval, built.messages),
                    temperature=settings.answer_temperature,
                    max_tokens=min(settings.answer_max_tokens, built.answer_tokens),
                ),
                caller=caller,
            )
        )
    except GatewayError as exc:
        await stack.aclose()
        raise _app_error(exc) from exc

    user_message = Message(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        role=MessageRole.USER,
        content=question,
        token_count=resources.counter.count(question),
        citations=[],
    )
    session.add(user_message)
    if conversation.title == DEFAULT_TITLE:
        conversation.title = " ".join(question.split())[:80]
    await session.commit()

    return PreparedChat(
        conversation_id=conversation.id,
        tenant_id=conversation.tenant_id,
        user_message_id=user_message.id,
        retrieval=retrieval,
        stream=stream,
        stack=stack,
        history=built.stats,
    )


async def stream_answer(resources: Resources, prepared: PreparedChat) -> AsyncIterator[bytes]:
    answer = _Answer()
    message_id: uuid.UUID | None = None
    try:
        try:
            async for chunk in prepared.stream:
                for choice in chunk.choices:
                    if choice.delta.content:
                        answer.parts.append(choice.delta.content)
                        yield _sse("token", {"text": choice.delta.content})
                if chunk.usage is not None:
                    answer.usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    }
        except (GatewayError, httpx.HTTPError, ValueError) as exc:
            code = exc.code if isinstance(exc, GatewayError) else "stream_error"
            answer.error = {"code": code, "message": str(exc)[:300]}
            yield _sse("error", answer.error)
        finally:
            await prepared.stack.aclose()

        citations, invalid = resolve_citations(answer.text, prepared.retrieval)
        yield _sse("citations", {"citations": [c.payload() for c in citations], "invalid": invalid})
        usage = _usage(prepared, answer)
        yield _sse("usage", usage)
        # Shielded: the answer is saved even if the client disconnects right now.
        message_id = await asyncio.shield(
            _save_answer(
                resources, prepared, answer, [c.stored() for c in citations], invalid, usage
            )
        )
        # Off the answer path: fold old turns into the summary when history grows.
        await asyncio.shield(
            history_service.schedule_compaction(
                resources, prepared.conversation_id, prepared.tenant_id
            )
        )
        yield _sse(
            "done",
            {"message_id": str(message_id), "user_message_id": str(prepared.user_message_id)},
        )
    finally:
        if message_id is None and answer.parts:
            # Client went away mid-stream: keep what was generated.
            citations, invalid = resolve_citations(answer.text, prepared.retrieval)
            await asyncio.shield(
                _save_answer(
                    resources,
                    prepared,
                    answer,
                    [c.stored() for c in citations],
                    invalid,
                    {**_usage(prepared, answer), "interrupted": True},
                )
            )


def _usage(prepared: PreparedChat, answer: _Answer) -> dict[str, Any]:
    meta = prepared.stream.meta
    prompt = answer.usage.get("prompt_tokens", 0)
    completion = answer.usage.get("completion_tokens") or estimate_tokens(answer.text)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "gateway": meta.model_dump(exclude_none=True),
        "retrieval_ms": prepared.retrieval.timings_ms,
        "sources": len(prepared.retrieval.chunks),
        "history": prepared.history,
        **({"error": answer.error} if answer.error else {}),
    }


async def _save_answer(
    resources: Resources,
    prepared: PreparedChat,
    answer: _Answer,
    citations: list[dict[str, Any]],
    invalid: list[int],
    usage: dict[str, Any],
) -> uuid.UUID:
    trace = prepared.retrieval.trace()
    trace["invalid_citations"] = invalid
    async with resources.sessionmaker() as session:
        message = Message(
            tenant_id=prepared.tenant_id,
            conversation_id=prepared.conversation_id,
            role=MessageRole.ASSISTANT,
            content=answer.text,
            token_count=resources.counter.count(answer.text),
            citations=citations,
            retrieval=trace,
            usage=usage,
        )
        session.add(message)
        # Bump the conversation so recent ones list first.
        await session.execute(
            update(Conversation)
            .where(Conversation.id == prepared.conversation_id)
            .values(updated_at=func.now())
        )
        await session.commit()
        return message.id


def _app_error(exc: GatewayError) -> AppError:
    err = AppError(exc.message)
    err.status_code = exc.status_code
    err.code = exc.code
    return err
