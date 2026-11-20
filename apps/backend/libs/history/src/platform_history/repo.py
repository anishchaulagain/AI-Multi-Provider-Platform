"""Loading conversation history state from Postgres."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_db.models import ConversationMemory, ConversationSummary, Message
from platform_history.builder import HistoryState
from platform_history.compress import HistoryTurn
from platform_history.memory import Memory


async def messages(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    rows = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.id)
    )
    return list(rows.scalars())


async def latest_summary(
    session: AsyncSession, conversation_id: uuid.UUID
) -> ConversationSummary | None:
    return (
        await session.execute(
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conversation_id)
            .order_by(ConversationSummary.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def memory(session: AsyncSession, conversation_id: uuid.UUID) -> Memory:
    row = await session.get(ConversationMemory, conversation_id)
    return Memory.model_validate(row.facts) if row and row.facts else Memory()


def covered_count(msgs: list[Message], summary: ConversationSummary | None) -> int:
    """How many leading messages the summary covers."""
    if summary is None:
        return 0
    for index, message in enumerate(msgs):
        if message.id == summary.covers_until_msg_id:
            return index + 1
    return 0


async def load_state(
    session: AsyncSession, conversation_id: uuid.UUID, *, capsule_text: str | None = None
) -> HistoryState:
    msgs = await messages(session, conversation_id)
    summary = await latest_summary(session, conversation_id)
    return HistoryState(
        turns=[HistoryTurn(str(m.id), m.role, m.content) for m in msgs],
        summary=summary.summary if summary else None,
        summarized_until=covered_count(msgs, summary),
        memory=await memory(session, conversation_id),
        capsule_text=capsule_text,
    )


async def unsummarized(session: AsyncSession, conversation_id: uuid.UUID) -> tuple[int, int]:
    """(messages, tokens) newer than the latest summary."""
    summary = await latest_summary(session, conversation_id)
    query = select(func.count(), func.coalesce(func.sum(Message.token_count), 0)).where(
        Message.conversation_id == conversation_id
    )
    if summary is not None:
        covered = await session.get(Message, summary.covers_until_msg_id)
        if covered is not None:
            query = query.where(
                (Message.created_at > covered.created_at)
                | ((Message.created_at == covered.created_at) & (Message.id > covered.id))
            )
    count, tokens = (await session.execute(query)).one()
    return int(count), int(tokens)
