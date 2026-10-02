import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_db.models import User, UserRole


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    email: str,
    password_hash: str,
    full_name: str | None = None,
    role: UserRole = UserRole.MEMBER,
) -> User:
    user = User(
        tenant_id=tenant_id,
        email=email.lower(),
        password_hash=password_hash,
        full_name=full_name,
        role=role,
    )
    session.add(user)
    await session.flush()
    return user
