import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, Header, Request, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ForbiddenError, RateLimitedError, UnauthorizedError
from app.core.resources import Resources, get_resources
from app.core.security import decode_access_token, hash_api_key
from app.models import User, UserRole
from app.repositories import api_keys as api_keys_repo
from app.repositories import users as users_repo


def _resources(request: Request) -> Resources:
    return get_resources(request.app)


ResourcesDep = Annotated[Resources, Depends(_resources)]


def get_app_settings(resources: ResourcesDep) -> Settings:
    return resources.settings


async def get_db(resources: ResourcesDep) -> AsyncGenerator[AsyncSession]:
    async with resources.sessionmaker() as session:
        yield session


def get_redis(resources: ResourcesDep) -> Redis:
    return resources.redis


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
RedisDep = Annotated[Redis, Depends(get_redis)]


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. Every tenant-scoped query must filter by tenant_id."""

    user: User
    auth_method: Literal["jwt", "api_key"]
    api_key_id: uuid.UUID | None = None

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.user.tenant_id

    @property
    def rate_limit_key(self) -> str:
        return f"apikey:{self.api_key_id}" if self.api_key_id else f"user:{self.user_id}"


async def _principal_from_api_key(session: AsyncSession, raw_key: str) -> Principal:
    api_key = await api_keys_repo.get_by_hash(session, hash_api_key(raw_key))
    now = datetime.now(UTC)
    if (
        api_key is None
        or api_key.revoked_at is not None
        or (api_key.expires_at is not None and api_key.expires_at <= now)
    ):
        raise UnauthorizedError("Invalid API key")
    user = await users_repo.get_by_id(session, api_key.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("Invalid API key")
    api_key.last_used_at = now
    await session.commit()
    return Principal(user=user, auth_method="api_key", api_key_id=api_key.id)


async def _principal_from_jwt(session: AsyncSession, token: str, settings: Settings) -> Principal:
    payload = decode_access_token(token, settings)
    try:
        user_id = uuid.UUID(payload["sub"])
    except ValueError as exc:
        raise UnauthorizedError("Invalid token") from exc
    user = await users_repo.get_by_id(session, user_id)
    if user is None or not user.is_active or str(user.tenant_id) != payload["tid"]:
        raise UnauthorizedError("Invalid token")
    return Principal(user=user, auth_method="jwt")


async def get_current_principal(
    session: DbSession,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Principal:
    if x_api_key:
        return await _principal_from_api_key(session, x_api_key)

    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            # Allow API keys as bearer tokens too (OpenAI-style clients).
            if token.startswith(settings.api_key_prefix):
                return await _principal_from_api_key(session, token)
            return await _principal_from_jwt(session, token, settings)

    raise UnauthorizedError("Not authenticated", headers={"WWW-Authenticate": "Bearer"})


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def require_admin(principal: CurrentPrincipal) -> Principal:
    if principal.user.role != UserRole.ADMIN:
        raise ForbiddenError("Admin role required")
    return principal


AdminPrincipal = Annotated[Principal, Depends(require_admin)]


async def rate_limit_principal(
    principal: CurrentPrincipal, resources: ResourcesDep, response: Response
) -> None:
    settings = resources.settings
    if not settings.rate_limit_enabled:
        return
    result = await resources.rate_limiter.hit(
        principal.rate_limit_key,
        capacity=settings.rate_limit_burst,
        per_minute=settings.rate_limit_per_minute,
    )
    if not result.allowed:
        raise RateLimitedError("Rate limit exceeded", headers=result.headers)
    response.headers.update(result.headers)


async def rate_limit_login(request: Request, resources: ResourcesDep) -> None:
    """Stricter, IP-based limit for unauthenticated login attempts."""
    if not resources.settings.rate_limit_enabled:
        return
    client_ip = request.client.host if request.client else "unknown"
    result = await resources.rate_limiter.hit(f"login:{client_ip}", capacity=5, per_minute=10)
    if not result.allowed:
        raise RateLimitedError("Too many login attempts", headers=result.headers)
