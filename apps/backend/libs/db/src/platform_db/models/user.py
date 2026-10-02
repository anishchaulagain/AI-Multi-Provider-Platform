import enum

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from platform_db.base import Base, IdMixin, TenantScopedMixin, TimestampMixin


class UserRole(enum.StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class User(IdMixin, TimestampMixin, TenantScopedMixin, Base):
    __tablename__ = "users"

    # Emails are stored lower-cased; unique platform-wide so login needs no tenant hint.
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
        default=UserRole.MEMBER,
    )
    is_active: Mapped[bool] = mapped_column(default=True)
