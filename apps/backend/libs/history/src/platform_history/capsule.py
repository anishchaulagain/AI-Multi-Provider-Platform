"""Context capsules: a compact, immutable snapshot of a conversation (summary, memory
facts, key Q/A and citation refs, typically 200-600 tokens) that can seed a new
conversation on any model or be attached to a request for agent-to-agent handoff."""

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from platform_db.models import Message
from platform_history.compress import HistoryTurn, compress_turn
from platform_history.memory import Memory
from platform_history.tokens import TokenCounter


class KeyExchange(BaseModel):
    question: str
    answer: str
    citations: list[dict[str, Any]] = Field(default_factory=list)


class CapsuleContent(BaseModel):
    version: Literal[1] = 1
    title: str
    summary: str = ""
    memory: Memory = Field(default_factory=Memory)
    key_exchanges: list[KeyExchange] = Field(default_factory=list)
    # Unique {doc_id, chunk_id} refs cited anywhere in the conversation.
    citations: list[dict[str, str]] = Field(default_factory=list)
    source_model: str | None = None
    message_count: int = 0


def render_capsule(content: CapsuleContent) -> str:
    """Text form used inside prompts (the capsule slot)."""
    parts = []
    if content.summary:
        parts.append(f"Summary: {content.summary}")
    facts = content.memory.render()
    if facts:
        parts.append(f"Facts:\n{facts}")
    if content.key_exchanges:
        qa = "\n".join(f"Q: {x.question}\nA: {x.answer}" for x in content.key_exchanges)
        parts.append(f"Key exchanges:\n{qa}")
    return "\n\n".join(parts)


def build_capsule(
    *,
    title: str,
    summary: str | None,
    memory: Memory,
    messages: Sequence[Message],
    source_model: str | None,
    counter: TokenCounter,
    max_exchanges: int = 4,
    max_question_tokens: int = 80,
    max_answer_tokens: int = 160,
) -> tuple[CapsuleContent, int]:
    pairs: list[tuple[Message, Message]] = [
        (q, a)
        for q, a in zip(messages, messages[1:], strict=False)
        if q.role == "user" and a.role == "assistant"
    ]
    # The latest exchanges, plus earlier ones that cite sources, newest preferred.
    chosen = pairs[-3:]
    for pair in reversed(pairs[:-3]):
        if len(chosen) >= max_exchanges:
            break
        if pair[1].citations:
            chosen.insert(0, pair)

    exchanges = []
    for q, a in chosen:
        answer = compress_turn(HistoryTurn(str(a.id), "assistant", a.content)).content
        exchanges.append(
            KeyExchange(
                question=counter.truncate(q.content.strip(), max_question_tokens),
                answer=counter.truncate(answer, max_answer_tokens),
                citations=[{"doc_id": c["doc_id"], "chunk_id": c["chunk_id"]} for c in a.citations],
            )
        )

    seen: set[str] = set()
    refs = []
    for m in messages:
        for c in m.citations:
            if c["chunk_id"] not in seen:
                seen.add(c["chunk_id"])
                refs.append({"doc_id": c["doc_id"], "chunk_id": c["chunk_id"]})

    content = CapsuleContent(
        title=title,
        summary=summary or "",
        memory=memory,
        key_exchanges=exchanges,
        citations=refs,
        source_model=source_model,
        message_count=len(messages),
    )
    return content, counter.count(render_capsule(content))
