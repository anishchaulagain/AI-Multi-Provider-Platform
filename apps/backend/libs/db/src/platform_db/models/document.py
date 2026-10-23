import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_db.base import Base, IdMixin, TenantScopedMixin, TimestampMixin


class DocumentStatus(enum.StrEnum):
    QUEUED = "queued"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (DocumentStatus.READY, DocumentStatus.FAILED)


class Document(IdMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        # Same bytes uploaded twice by one tenant -> one document.
        UniqueConstraint("tenant_id", "content_hash", name="uq_documents_tenant_id_content_hash"),
        Index("ix_documents_tenant_id_created_at", "tenant_id", "created_at"),
    )

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column()
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex
    storage_key: Mapped[str] = mapped_column(String(512))
    # Plain string (not a PG enum) so later phases can add stages without a type migration.
    status: Mapped[str] = mapped_column(String(16), default=DocumentStatus.QUEUED, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str | None] = mapped_column(String(500))
    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # Extracted summary/keywords and other pipeline output.
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
