from fastapi import APIRouter, Depends

from app.api.deps import rate_limit_principal
from app.api.routes import api_keys, auth

api_router = APIRouter()

# Public auth endpoints (login has its own IP-based limiter).
api_router.include_router(auth.router)

# Authenticated, per-principal rate limited.
protected = APIRouter(dependencies=[Depends(rate_limit_principal)])
protected.include_router(api_keys.router)
api_router.include_router(protected)
