"""Structured conversation memory: short atomic facts with their source messages.

Compaction proposes a `MemoryPatch` (validated with Pydantic); `apply_patch` merges
it deterministically, so a bad model answer can't corrupt the stored memory.
"""

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["entities", "user_preferences", "decisions", "open_questions", "referenced_docs"]
CATEGORIES: tuple[Category, ...] = (
    "entities",
    "user_preferences",
    "decisions",
    "open_questions",
    "referenced_docs",
)
_LABELS = {
    "entities": "Entities",
    "user_preferences": "User preferences",
    "decisions": "Decisions",
    "open_questions": "Open questions",
    "referenced_docs": "Referenced documents",
}
MAX_FACT_CHARS = 240


class Fact(BaseModel):
    text: str
    source_msg_ids: list[str] = Field(default_factory=list)


class Memory(BaseModel):
    entities: list[Fact] = Field(default_factory=list)
    user_preferences: list[Fact] = Field(default_factory=list)
    decisions: list[Fact] = Field(default_factory=list)
    open_questions: list[Fact] = Field(default_factory=list)
    referenced_docs: list[Fact] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(getattr(self, c) for c in CATEGORIES)

    def render(self) -> str:
        """Compact text form for prompts: one line per fact, grouped by category."""
        lines = []
        for category in CATEGORIES:
            facts = getattr(self, category)
            if facts:
                lines.append(f"{_LABELS[category]}: " + "; ".join(f.text for f in facts))
        return "\n".join(lines)


class FactChange(BaseModel):
    category: Category
    text: str = Field(description="One short, self-contained statement")
    source_msg_ids: list[str] = Field(
        default_factory=list, description="Ids of the messages the fact comes from"
    )


class MemoryPatch(BaseModel):
    add: list[FactChange] = Field(default_factory=list)
    remove: list[FactChange] = Field(
        default_factory=list, description="Facts that are no longer true or relevant"
    )


def _key(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".")


def apply_patch(memory: Memory, patch: MemoryPatch, *, max_per_category: int = 15) -> Memory:
    data = memory.model_copy(deep=True)
    for change in patch.remove:
        facts: list[Fact] = getattr(data, change.category)
        target = _key(change.text)
        setattr(data, change.category, [f for f in facts if _key(f.text) != target])
    for change in patch.add:
        text = " ".join(change.text.split())[:MAX_FACT_CHARS]
        if not text:
            continue
        facts = getattr(data, change.category)
        existing = next((f for f in facts if _key(f.text) == _key(text)), None)
        if existing is not None:
            existing.source_msg_ids = sorted(
                set(existing.source_msg_ids) | set(change.source_msg_ids)
            )
            continue
        facts.append(Fact(text=text, source_msg_ids=change.source_msg_ids))
        # Oldest facts drop first when a category overflows.
        setattr(data, change.category, facts[-max_per_category:])
    return data
