import uuid

from fastapi import APIRouter, status

from platform_api.api.deps import CurrentPrincipal, DbSession, SettingsDep
from platform_api.repositories import api_keys as api_keys_repo
from platform_api.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyRead
from platform_api.services import api_keys as api_keys_service

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreate, principal: CurrentPrincipal, session: DbSession, settings: SettingsDep
) -> ApiKeyCreated:
    api_key, raw_key = await api_keys_service.create_api_key(
        session,
        settings=settings,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        name=body.name,
        expires_in_days=body.expires_in_days,
    )
    return ApiKeyCreated(**ApiKeyRead.model_validate(api_key).model_dump(), key=raw_key)


@router.get("", response_model=list[ApiKeyRead])
async def list_api_keys(principal: CurrentPrincipal, session: DbSession) -> list[ApiKeyRead]:
    keys = await api_keys_repo.list_for_user(session, principal.user_id)
    return [ApiKeyRead.model_validate(k) for k in keys]


@router.delete("/{api_key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    api_key_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession
) -> None:
    await api_keys_service.revoke_api_key(session, api_key_id=api_key_id, user_id=principal.user_id)
