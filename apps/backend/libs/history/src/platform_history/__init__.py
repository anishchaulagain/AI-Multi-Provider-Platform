"""Token-budgeted conversation history (Phase 5): context builder, background
compaction, semantic recall and shareable context capsules."""

from platform_history.builder import BuiltHistory, ContextBuilder, HistoryState, SlotShares
from platform_history.capsule import CapsuleContent, build_capsule, render_capsule
from platform_history.compaction import CompactionResult, compact_conversation
from platform_history.compress import HistoryTurn
from platform_history.memory import Memory, MemoryPatch, apply_patch
from platform_history.tokens import TokenCounter
from platform_history.turns import TurnStore

__all__ = [
    "BuiltHistory",
    "CapsuleContent",
    "CompactionResult",
    "ContextBuilder",
    "HistoryState",
    "HistoryTurn",
    "Memory",
    "MemoryPatch",
    "SlotShares",
    "TokenCounter",
    "TurnStore",
    "apply_patch",
    "build_capsule",
    "compact_conversation",
    "render_capsule",
]
