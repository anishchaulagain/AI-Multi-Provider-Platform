"""Background compaction: fold older turns into a bounded rolling summary, patch the
structured memory, and embed the turns for semantic recall.

    new_summary = summarize(previous_summary + memory + turns_since)   (summarize alias)

The summary is hard-capped (default 300 tokens) however long the chat grows. The
newest `keep_recent` messages are left out: they stay verbatim in the recent window.
"""

import uuid
from dataclasses import dataclass

import structlog
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from llm_client import Caller, ChatMessage, GatewayClient, GatewayError
from platform_db.models import ConversationMemory, ConversationSummary, Message
from platform_history import repo
from platform_history.compress import HistoryTurn, compress_turn, short_id
from platform_history.memory import Memory, MemoryPatch, apply_patch
from platform_history.tokens import TokenCounter
from platform_history.turns import TurnPair, TurnStore

logger = structlog.get_logger(__name__)

_MAX_TURN_CHARS = 3000
_EMBED_CHARS = 2000


class SummaryUpdate(BaseModel):
    summary: str = Field(
        description="Updated summary of the whole conversation so far, in plain prose"
    )
    memory: MemoryPatch = Field(default_factory=MemoryPatch)


_SYSTEM = (
    "You maintain the long-term memory of a conversation between a user and an assistant.\n"
    "Given the previous summary, the current memory facts and the new messages, return:\n"
    "- summary: an updated summary of the WHOLE conversation (previous summary + new "
    "messages), at most {max_words} words. Keep what only this conversation knows, word "
    "for word where it matters: the user's names, codes, numbers, dates, goals, decisions "
    "and preferences. Answers that came from the documents can be retrieved again: "
    "mention only the topics asked about, not the answers.\n"
    "- memory: a patch to the facts. `add` short atomic facts the USER stated or decided "
    "(category: entities, user_preferences, decisions, open_questions or referenced_docs) "
    "with the ids of the messages they come from. Do not add facts that come from document "
    "answers; for those, at most note the document under referenced_docs. `remove` facts "
    "that are now outdated or answered."
)


@dataclass
class CompactionResult:
    summary: str
    summary_tokens: int
    covered_messages: int
    embedded_pairs: int
    version: int


def _format(messages: list[Message]) -> str:
    lines = []
    for m in messages:
        turn = compress_turn(HistoryTurn(str(m.id), m.role, m.content))
        lines.append(f"[msg {short_id(str(m.id))}] {m.role}: {turn.content[:_MAX_TURN_CHARS]}")
    return "\n\n".join(lines)


def pair_turns(conversation_id: uuid.UUID, msgs: list[Message], start_seq: int) -> list[TurnPair]:
    pairs: list[TurnPair] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m.role == "user":
            answer = msgs[i + 1] if i + 1 < len(msgs) and msgs[i + 1].role == "assistant" else None
            q = compress_turn(HistoryTurn(str(m.id), "user", m.content)).content
            text = f"User: {q}"
            if answer is not None:
                a = compress_turn(HistoryTurn(str(answer.id), "assistant", answer.content)).content
                text += f"\nAssistant: {a}"
            pairs.append(
                TurnPair(
                    conversation_id,
                    str(m.id),
                    str(answer.id) if answer else None,
                    text[:_EMBED_CHARS],
                    start_seq + i,
                )
            )
            i += 2 if answer is not None else 1
        else:
            i += 1
    return pairs


async def summarize(
    gateway: GatewayClient,
    caller: Caller,
    *,
    previous: str | None,
    memory: Memory,
    new_messages: list[Message],
    alias: str,
    max_tokens: int,
    counter: TokenCounter,
) -> tuple[str, MemoryPatch]:
    max_words = max(40, int(max_tokens * 0.7))
    prompt = (
        f"Previous summary:\n{previous or '(none)'}\n\n"
        f"Current memory facts:\n{memory.render() or '(none)'}\n\n"
        f"New messages:\n{_format(new_messages)}"
    )
    result = await gateway.judge(
        [
            ChatMessage(role="system", content=_SYSTEM.format(max_words=max_words)),
            ChatMessage(role="user", content=prompt),
        ],
        SummaryUpdate,
        caller=caller,
        model=alias,
    )
    summary = counter.truncate(" ".join(result.data.summary.split()), max_tokens)
    return summary, result.data.memory


async def compact_conversation(
    sessionmaker: async_sessionmaker[AsyncSession],
    gateway: GatewayClient,
    turns: TurnStore,
    counter: TokenCounter,
    *,
    conversation_id: uuid.UUID,
    tenant_id: uuid.UUID,
    caller: Caller,
    alias: str = "summarize",
    embed_alias: str = "embed",
    embed_prefix: str = "",
    summary_max_tokens: int = 300,
    keep_recent: int = 6,
) -> CompactionResult | None:
    """Summarize everything except the newest `keep_recent` messages. Returns None
    when there is nothing new to fold in. Gateway errors propagate (the queue retries)."""
    async with sessionmaker() as session:
        msgs = await repo.messages(session, conversation_id)
        previous = await repo.latest_summary(session, conversation_id)
        memory = await repo.memory(session, conversation_id)
    start = repo.covered_count(msgs, previous)
    end = len(msgs) - keep_recent
    # Never end a compacted span between a question and its answer.
    if 0 < end < len(msgs) and msgs[end].role == "assistant":
        end += 1
    new = msgs[start:end]
    if not new:
        return None

    summary, patch = await summarize(
        gateway,
        caller,
        previous=previous.summary if previous else None,
        memory=memory,
        new_messages=new,
        alias=alias,
        max_tokens=summary_max_tokens,
        counter=counter,
    )
    # Fact sources are short ids in the prompt; map them back to full message ids.
    by_short = {short_id(str(m.id)): str(m.id) for m in msgs}
    for change in patch.add:
        change.source_msg_ids = [by_short.get(s, s) for s in change.source_msg_ids]
    updated = apply_patch(memory, patch)

    pairs = pair_turns(conversation_id, new, start)
    if pairs:
        vectors = await gateway.embed(
            [f"{embed_prefix}{p.text}" for p in pairs], caller=caller, model=embed_alias
        )
        data = sorted(vectors.data.data, key=lambda d: d.index)
        await turns.add(tenant_id, pairs, [d.embedding for d in data])

    version = (previous.version if previous else 0) + 1
    async with sessionmaker() as session:
        row = await session.get(ConversationMemory, conversation_id)
        if row is None:
            row = ConversationMemory(conversation_id=conversation_id, tenant_id=tenant_id)
            session.add(row)
        row.facts = updated.model_dump()
        row.version = (row.version or 0) + 1
        session.add(
            ConversationSummary(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                version=version,
                summary=summary,
                token_count=counter.count(summary),
                covers_until_msg_id=new[-1].id,
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            # Another compaction of this conversation (worker vs. capsule creation)
            # wrote this version first; its summary is just as current. Or the
            # conversation was deleted meanwhile. Either way, nothing to add.
            await session.rollback()
            logger.info("compaction_superseded", conversation_id=str(conversation_id))
            return None

    logger.info(
        "conversation_compacted",
        conversation_id=str(conversation_id),
        version=version,
        covered=len(new),
        summary_tokens=counter.count(summary),
        pairs=len(pairs),
    )
    return CompactionResult(
        summary=summary,
        summary_tokens=counter.count(summary),
        covered_messages=start + len(new),
        embedded_pairs=len(pairs),
        version=version,
    )


__all__ = [
    "CompactionResult",
    "GatewayError",
    "SummaryUpdate",
    "ValidationError",
    "compact_conversation",
    "pair_turns",
    "summarize",
]
