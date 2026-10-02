from fastapi import APIRouter, Depends

from platform_api.api.deps import CurrentPrincipal, DbSession, SettingsDep, rate_limit_login
from platform_api.core.security import create_access_token
from platform_api.schemas.auth import LoginRequest, TokenResponse
from platform_api.schemas.user import UserRead
from platform_api.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(rate_limit_login)])
async def login(body: LoginRequest, session: DbSession, settings: SettingsDep) -> TokenResponse:
    user = await auth_service.authenticate(session, email=body.email, password=body.password)
    token, expires_in = create_access_token(
        user_id=user.id, tenant_id=user.tenant_id, settings=settings
    )
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=UserRead)
async def me(principal: CurrentPrincipal) -> UserRead:
    return UserRead.model_validate(principal.user)
