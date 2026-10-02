import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from platform_db.base import Base, IdMixin, TenantScopedMixin


class LlmUsage(IdMixin, TenantScopedMixin, Base):
    """One row per gateway request (including cache hits and failures)."""

    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_tenant_id_created_at", "tenant_id", "created_at"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    request_id: Mapped[str | None] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))  # chat | embedding
    alias: Mapped[str] = mapped_column(String(64))
    deployment: Mapped[str | None] = mapped_column(String(128))
    provider: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))  # ok | error | cache_hit | blocked
    cache: Mapped[str] = mapped_column(String(16), default="miss")  # miss | exact | semantic | off
    fallbacks: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
