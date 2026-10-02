import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.security import generate_api_key, hash_api_key
from app.models import ApiKey
from app.repositories import api_keys as api_keys_repo


async def create_api_key(
    session: AsyncSession,
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    expires_in_days: int | None,
) -> tuple[ApiKey, str]:
    raw_key = generate_api_key(settings.api_key_prefix)
    expires_at = datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None
    api_key = await api_keys_repo.create(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        name=name,
        key_prefix=raw_key[: len(settings.api_key_prefix) + 6],
        key_hash=hash_api_key(raw_key),
        expires_at=expires_at,
    )
    await session.commit()
    return api_key, raw_key


async def revoke_api_key(
    session: AsyncSession, *, api_key_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    api_key = await api_keys_repo.get_for_user(session, api_key_id=api_key_id, user_id=user_id)
    if api_key is None:
        raise NotFoundError("API key not found")
    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(UTC)
        await session.commit()
