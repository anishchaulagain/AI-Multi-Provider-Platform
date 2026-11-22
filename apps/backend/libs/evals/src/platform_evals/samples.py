"""Rebuild what an answer was based on, and store its scores.

Messages keep only chunk ids (never the retrieved text), in source order inside the
retrieval trace, so the numbered sources the model saw are fetched back from Qdrant.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from platform_db.models import EvalResult, EvalSource, Message, MessageRole
from platform_evals.evaluator import EvalSample, EvalScores
from platform_evals.metrics import Source
from platform_rag.store import ChunkStore

MISSING_SOURCE = "(this source is no longer available)"


class SampleUnavailableError(Exception):
    """The message can't be evaluated (gone, not an answer, or its sources were deleted)."""


@dataclass(frozen=True)
class LoadedSample:
    message: Message
    sample: EvalSample


async def load_sample(
    session: AsyncSession, chunks: ChunkStore, message_id: uuid.UUID
) -> LoadedSample:
    message = await session.get(Message, message_id)
    if message is None or message.role != MessageRole.ASSISTANT:
        raise SampleUnavailableError("not an assistant message")
    if not message.content.strip():
        raise SampleUnavailableError("empty answer")
    question = (
        await session.execute(
            select(Message.content)
            .where(
                Message.conversation_id == message.conversation_id,
                Message.role == MessageRole.USER,
                Message.created_at <= message.created_at,
                Message.id != message.id,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if question is None:
        raise SampleUnavailableError("no question before this answer")

    ids = [c["chunk_id"] for c in (message.retrieval or {}).get("chunks", [])]
    found = {c.id: c for c in await chunks.get_chunks(message.tenant_id, ids)}
    if ids and not found:
        raise SampleUnavailableError("the answer's sources were deleted")
    sources = []
    for n, chunk_id in enumerate(ids, start=1):
        chunk = found.get(chunk_id)
        sources.append(
            Source(
                n=n,
                text=chunk.text if chunk else MISSING_SOURCE,
                chunk_id=chunk_id,
                doc_id=chunk.doc_id if chunk else "",
                doc_title=chunk.doc_title if chunk else "",
            )
        )
    gateway = (message.usage or {}).get("gateway") or {}
    answered_by = {
        k: str(gateway[k]) for k in ("alias", "deployment", "provider") if gateway.get(k)
    }
    return LoadedSample(
        message=message,
        sample=EvalSample(
            question=question, answer=message.content, sources=sources, answered_by=answered_by
        ),
    )


async def save_result(
    session: AsyncSession,
    loaded: LoadedSample,
    scores: EvalScores,
    *,
    source: EvalSource,
    run_id: str | None = None,
) -> uuid.UUID:
    """Insert or replace the message's evaluation; returns the result id."""
    answered_by = loaded.sample.answered_by
    values = {
        "source": source.value,
        "run_id": run_id,
        **scores.as_dict(),
        "answer_alias": answered_by.get("alias"),
        "answer_deployment": answered_by.get("deployment"),
        "answer_provider": answered_by.get("provider"),
        "judge_deployment": ",".join(scores.judges)[:256] or None,
        "details": scores.details,
        "error": None,
    }
    statement = (
        insert(EvalResult)
        .values(
            id=uuid7(),
            tenant_id=loaded.message.tenant_id,
            message_id=loaded.message.id,
            **values,
        )
        .on_conflict_do_update(index_elements=[EvalResult.message_id], set_=values)
        .returning(EvalResult.id)
    )
    result_id = (await session.execute(statement)).scalar_one()
    await session.commit()
    return result_id
