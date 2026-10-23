"""Grounded answering: numbered-source prompt and citation parsing/validation."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from llm_client import ChatMessage
from platform_rag.retrieve import RetrievalResult, Turn

NO_ANSWER = "I don't know based on the provided documents."

SYSTEM_PROMPT = f"""You are a careful assistant that answers questions using only the \
numbered sources provided.

Rules:
- Support every factual statement with citations like [1] or [2][3], placed right \
after the statement they support. Cite only source numbers that appear below.
- Use only information from the sources. Do not rely on prior knowledge.
- If the sources do not contain the answer, reply exactly: "{NO_ANSWER}"
- "Graph facts" are relationships extracted from the same documents; use them as \
hints, but cite the numbered source that states the fact.
- Be concise. Use the language of the question."""

# [1], [1, 2] and the full-width 【1】 some models (e.g. gpt-oss) emit.
_CITATION = re.compile(r"[\[【](\d{1,3}(?:\s*[,，、]\s*\d{1,3})*)[\]】]")
_SEPARATORS = re.compile(r"[,，、]")
_SNIPPET_CHARS = 280


@dataclass(frozen=True)
class Citation:
    n: int
    chunk_id: str
    doc_id: str
    doc_title: str
    page: int | None
    section: str | None
    snippet: str
    text: str = ""

    def stored(self) -> dict[str, Any]:
        """What persists with the message: references only, never the text."""
        return {"n": self.n, "chunk_id": self.chunk_id, "doc_id": self.doc_id}

    def payload(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "page": self.page,
            "section": self.section,
            "snippet": self.snippet,
            "text": self.text,
        }


def format_sources(result: RetrievalResult) -> str:
    parts = []
    for n, item in enumerate(result.chunks, start=1):
        chunk = item.chunk
        where = ", ".join(
            p
            for p in (
                chunk.doc_title,
                f"page {chunk.page}" if chunk.page else None,
                chunk.section,
            )
            if p
        )
        parts.append(f"[{n}] ({where})\n{chunk.text}")
    text = "\n\n".join(parts) or "(no sources found)"
    if result.facts:
        text += "\n\nGraph facts:\n" + "\n".join(f"- {f.text()}" for f in result.facts)
    return text


def build_messages(
    question: str, result: RetrievalResult, history: Sequence[Turn] = ()
) -> list[ChatMessage]:
    messages = [ChatMessage(role="system", content=SYSTEM_PROMPT)]
    # Recent turns give conversational continuity; sources stay the only evidence.
    for turn in list(history)[-4:]:
        messages.append(ChatMessage(role=turn.role, content=turn.content[:2000]))
    messages.append(
        ChatMessage(
            role="user",
            content=f"Sources:\n\n{format_sources(result)}\n\nQuestion: {question}",
        )
    )
    return messages


def parse_citations(text: str) -> list[int]:
    """Source numbers in order of first appearance: "[1][3]" and "[1, 3]" both count."""
    seen: list[int] = []
    for match in _CITATION.finditer(text):
        for part in _SEPARATORS.split(match.group(1)):
            n = int(part.strip())
            if n not in seen:
                seen.append(n)
    return seen


def resolve_citations(text: str, result: RetrievalResult) -> tuple[list[Citation], list[int]]:
    """Map [n] to the chunk that was provided as source n. Returns (valid, invalid):
    numbers outside the provided sources are dropped, since the model made them up."""
    valid: list[Citation] = []
    invalid: list[int] = []
    for n in parse_citations(text):
        if not 1 <= n <= len(result.chunks):
            invalid.append(n)
            continue
        chunk = result.chunks[n - 1].chunk
        snippet = " ".join(chunk.text.split())
        valid.append(
            Citation(
                n=n,
                chunk_id=chunk.id,
                doc_id=chunk.doc_id,
                doc_title=chunk.doc_title,
                page=chunk.page,
                section=chunk.section,
                snippet=snippet[:_SNIPPET_CHARS] + ("…" if len(snippet) > _SNIPPET_CHARS else ""),
                text=chunk.text,
            )
        )
    return valid, invalid
