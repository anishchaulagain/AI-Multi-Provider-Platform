import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from platform_db.base import Base, IdMixin, TenantScopedMixin, TimestampMixin


class ApiKey(IdMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "api_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    # First characters of the raw key, shown in the UI to identify it.
    key_prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
