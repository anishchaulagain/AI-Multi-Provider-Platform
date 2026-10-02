from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from platform_db.base import Base, IdMixin, TimestampMixin


class Tenant(IdMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    # Daily LLM token budget enforced by the gateway; NULL uses the gateway default.
    daily_token_quota: Mapped[int | None] = mapped_column(BigInteger)
