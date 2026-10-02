import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey


async def create(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    key_prefix: str,
    key_hash: str,
    expires_at: datetime | None,
) -> ApiKey:
    api_key = ApiKey(
        tenant_id=tenant_id,
        user_id=user_id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        expires_at=expires_at,
    )
    session.add(api_key)
    await session.flush()
    await session.refresh(api_key)
    return api_key


async def get_by_hash(session: AsyncSession, key_hash: str) -> ApiKey | None:
    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    return result.scalar_one_or_none()


async def get_for_user(
    session: AsyncSession, *, api_key_id: uuid.UUID, user_id: uuid.UUID
) -> ApiKey | None:
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == api_key_id, ApiKey.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def list_for_user(session: AsyncSession, user_id: uuid.UUID) -> Sequence[ApiKey]:
    result = await session.execute(
        select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc())
    )
    return result.scalars().all()
