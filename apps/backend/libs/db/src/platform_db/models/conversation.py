import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_db.base import Base, IdMixin, TenantScopedMixin, TimestampMixin


class MessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class Conversation(IdMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_id_updated_at", "user_id", "updated_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    # Restrict retrieval to these documents; empty means all of the tenant's documents.
    doc_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # Answer with this gateway alias instead of the default (set when forking a capsule).
    alias: Mapped[str | None] = mapped_column(String(64))
    # Seeded from a context capsule: its summary and facts are part of every prompt.
    capsule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("context_capsules.id", ondelete="SET NULL", use_alter=True)
    )


class Message(IdMixin, TenantScopedMixin, Base):
    """Raw turns, kept as the source of truth. Retrieved context text is never stored:
    citations hold chunk ids only."""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    # [{"n": 1, "chunk_id": ..., "doc_id": ...}]
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # Rewritten query, candidates with their scores, graph seeds/facts, timings.
    retrieval: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Token usage and gateway routing (alias, deployment, provider, cache).
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
