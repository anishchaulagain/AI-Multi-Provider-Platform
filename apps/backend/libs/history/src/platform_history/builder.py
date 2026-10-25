"""Token-budgeted context builder.

The prompt window is split into slots (shares of the target model's window):

    system 5% | memory 5% | summary 10% | recall 10% | recent 20% | RAG 35% | answer 15%

Unused budget in any history slot rolls over to RAG. Slots are emitted in a stable
order (system -> memory+summary -> recall -> recent turns), so the long-lived prefix
stays identical between requests and providers can reuse their prompt cache.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from llm_client import ChatMessage
from platform_history.compress import HistoryTurn, compress_turn, dedupe
from platform_history.memory import Memory
from platform_history.tokens import TokenCounter

# (question, message ids to exclude, k) -> older turns, best first
RecallFn = Callable[[str, set[str], int], Awaitable[list[HistoryTurn]]]

_MIN_ANSWER_TOKENS = 64
_PER_MESSAGE_OVERHEAD = 4  # role/formatting tokens per chat message


@dataclass(frozen=True)
class SlotShares:
    system: float = 0.05
    memory: float = 0.05
    summary: float = 0.10
    recall: float = 0.10
    recent: float = 0.20
    rag: float = 0.35
    answer: float = 0.15


@dataclass
class HistoryState:
    """Everything known about a conversation before this turn."""

    turns: list[HistoryTurn] = field(default_factory=list)  # oldest first
    summary: str | None = None
    summarized_until: int = 0  # leading turns covered by the summary (and embedded)
    memory: Memory = field(default_factory=Memory)
    capsule_text: str | None = None  # a context capsule this conversation builds on


@dataclass
class BuiltHistory:
    messages: list[ChatMessage]  # goes between the system prompt and the question
    rag_tokens: int
    answer_tokens: int
    stats: dict[str, Any]

    @property
    def recent_turns(self) -> list[ChatMessage]:
        return [m for m in self.messages if m.role in ("user", "assistant")]


class ContextBuilder:
    def __init__(
        self,
        counter: TokenCounter,
        shares: SlotShares | None = None,
        *,
        recall_k: int = 3,
        max_recent_messages: int = 10,
        max_code_lines: int = 12,
    ) -> None:
        self.counter = counter
        self.shares = shares or SlotShares()
        self.recall_k = recall_k
        # Last N messages verbatim, within the recent budget; older ones are
        # summarized by compaction (which keeps fewer than N, so none fall between).
        self.max_recent_messages = max_recent_messages
        self.max_code_lines = max_code_lines

    def budgets(self, window: int) -> dict[str, int]:
        s = self.shares
        out = {
            name: int(window * getattr(s, name))
            for name in ("system", "memory", "summary", "recall", "recent", "rag", "answer")
        }
        out["answer"] = min(max(out["answer"], _MIN_ANSWER_TOKENS), window // 2)
        return out

    def _tokens(self, messages: Sequence[ChatMessage]) -> int:
        return sum(self.counter.count(m.text()) + _PER_MESSAGE_OVERHEAD for m in messages)

    async def build(
        self,
        state: HistoryState,
        question: str,
        *,
        window: int,
        system_tokens: int,
        recall: RecallFn | None = None,
    ) -> BuiltHistory:
        budget = self.budgets(window)
        count = self.counter.count
        spare = budget["system"] - system_tokens  # may be negative: taken from RAG

        # ---- memory + summary (stable prefix) ----
        memory_text = self.counter.truncate(state.memory.render(), budget["memory"])
        summary_parts = []
        if state.capsule_text:
            summary_parts.append(f"Shared context capsule:\n{state.capsule_text}")
        if state.summary:
            summary_parts.append(state.summary)
        summary_text = self.counter.truncate("\n\n".join(summary_parts), budget["summary"])
        spare += budget["memory"] - count(memory_text) + budget["summary"] - count(summary_text)

        # ---- recent window: newest turns verbatim (compressed), oldest dropped first ----
        unsummarized = [
            compress_turn(t, max_code_lines=self.max_code_lines)
            for t in state.turns[state.summarized_until :]
        ]
        unsummarized = dedupe(unsummarized)
        recent: list[HistoryTurn] = []
        used = 0
        for turn in reversed(unsummarized):
            if len(recent) >= self.max_recent_messages:
                break
            cost = count(turn.content) + _PER_MESSAGE_OVERHEAD
            if used + cost > budget["recent"]:
                if not recent and budget["recent"] > _PER_MESSAGE_OVERHEAD + 8:
                    # A single huge latest turn: keep its head and tail.
                    limit = budget["recent"] - _PER_MESSAGE_OVERHEAD
                    content = self.counter.truncate(turn.content, limit, keep_tail=True)
                    recent.append(HistoryTurn(turn.msg_id, turn.role, content))
                    used += count(content) + _PER_MESSAGE_OVERHEAD
                break
            recent.append(turn)
            used += cost
        recent.reverse()
        dropped = len(unsummarized) - len(recent)
        spare += budget["recent"] - used

        # ---- semantic recall: older turns relevant to this question ----
        recalled: list[HistoryTurn] = []
        recall_used = 0
        if recall is not None and state.summarized_until > 0 and budget["recall"] > 0:
            exclude = {t.msg_id for t in recent}
            for turn in await recall(question, exclude, self.recall_k):
                compressed = compress_turn(turn, max_code_lines=self.max_code_lines)
                cost = count(compressed.content) + 2
                if recall_used + cost > budget["recall"]:
                    continue
                recalled.append(compressed)
                recall_used += cost
        spare += budget["recall"] - recall_used

        # ---- assemble in stable order ----
        messages: list[ChatMessage] = []
        prefix = []
        if memory_text:
            prefix.append(f"Conversation memory (facts so far):\n{memory_text}")
        if summary_text:
            prefix.append(f"Summary of the earlier conversation:\n{summary_text}")
        if prefix:
            messages.append(ChatMessage(role="system", content="\n\n".join(prefix)))
        if recalled:
            messages.append(
                ChatMessage(
                    role="system",
                    content="Earlier exchanges relevant to the new question:\n\n"
                    + "\n\n".join(t.content for t in recalled),
                )
            )
        messages.extend(ChatMessage(role=t.role, content=t.content) for t in recent)

        raw = sum(count(t.content) + _PER_MESSAGE_OVERHEAD for t in state.turns)
        sent = self._tokens(messages)
        rag = max(0, budget["rag"] + spare)
        return BuiltHistory(
            messages=messages,
            rag_tokens=rag,
            answer_tokens=budget["answer"],
            stats={
                "window": window,
                "raw_history_tokens": raw,
                "sent_history_tokens": sent,
                "history_tokens_saved_ratio": round(1 - sent / raw, 4) if raw else 0.0,
                "slots": {
                    "memory": count(memory_text),
                    "summary": count(summary_text),
                    "recall": recall_used,
                    "recent": used,
                    "rag": rag,
                    "answer": budget["answer"],
                },
                "recent_turns": len(recent),
                "recalled_turns": len(recalled),
                "dropped_turns": dropped,
            },
        )
