import enum
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_db.base import Base, IdMixin, TenantScopedMixin, TimestampMixin


class EvalSource(enum.StrEnum):
    ONLINE = "online"  # sampled live answer, scored by the eval worker
    OFFLINE = "offline"  # golden dataset run (`make eval`)
    MANUAL = "manual"  # an admin asked for it


class EvalResult(IdMixin, TimestampMixin, TenantScopedMixin, Base):
    """LLM-judge scores for one assistant message (latest evaluation wins). Scores are
    0..1, or null when the metric doesn't apply (e.g. no citations to check)."""

    __tablename__ = "eval_results"
    __table_args__ = (Index("ix_eval_results_tenant_id_created_at", "tenant_id", "created_at"),)

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), unique=True
    )
    source: Mapped[str] = mapped_column(String(16))
    # Groups the results of one offline run.
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    faithfulness: Mapped[float | None] = mapped_column(Float)
    answer_relevance: Mapped[float | None] = mapped_column(Float)
    context_precision: Mapped[float | None] = mapped_column(Float)
    citation_accuracy: Mapped[float | None] = mapped_column(Float)
    # Who answered and who judged (gateway alias / deployment / provider).
    answer_alias: Mapped[str | None] = mapped_column(String(64))
    answer_deployment: Mapped[str | None] = mapped_column(String(128), index=True)
    answer_provider: Mapped[str | None] = mapped_column(String(64))
    judge_deployment: Mapped[str | None] = mapped_column(String(256))
    # Per-metric verdicts and reasons (claims, relevant sources, cited sentences).
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class MessageFeedback(IdMixin, TimestampMixin, TenantScopedMixin, Base):
    """A user's thumbs up (1) or down (-1) on an assistant message, with a comment."""

    __tablename__ = "message_feedback"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_message_feedback_message_user"),
        CheckConstraint("rating IN (-1, 1)", name="rating_sign"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    rating: Mapped[int] = mapped_column(SmallInteger)
    comment: Mapped[str | None] = mapped_column(Text)
