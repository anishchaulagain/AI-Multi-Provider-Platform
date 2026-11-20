import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_db.base import Base, IdMixin, TenantScopedMixin, TimestampMixin


class ConversationSummary(IdMixin, TenantScopedMixin, Base):
    """Rolling summary of a conversation. Each compaction adds a new version; the
    latest one covers every message up to and including `covers_until_msg_id`."""

    __tablename__ = "conversation_summaries"
    __table_args__ = (
        UniqueConstraint("conversation_id", "version", name="uq_conversation_summaries_version"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    covers_until_msg_id: Mapped[uuid.UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationMemory(TimestampMixin, TenantScopedMixin, Base):
    """Structured facts ({entities, user_preferences, decisions, open_questions,
    referenced_docs}), each a short statement with the message ids it came from."""

    __tablename__ = "conversation_memory"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=0)


class ContextCapsule(IdMixin, TenantScopedMixin, Base):
    """Immutable, shareable snapshot of a conversation's state (summary, facts, key
    Q/A, citation refs), small enough to seed a new conversation on any model."""

    __tablename__ = "context_capsules"
    __table_args__ = (
        Index("ix_context_capsules_created_by_created_at", "created_by", "created_at"),
    )

    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    token_count: Mapped[int] = mapped_column(Integer)
    source_model: Mapped[str | None] = mapped_column(String(128))
    share_token: Mapped[str] = mapped_column(String(128), unique=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
