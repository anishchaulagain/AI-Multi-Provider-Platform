import secrets
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from gateway.resources import Resources
from gateway.service import Caller, GatewayService, InvalidRequestError
from llm_client import headers as h
from llm_client.types import ChatCompletion, ChatCompletionRequest, EmbeddingRequest, ModelInfo
from platform_core.errors import UnauthorizedError
from platform_core.health import HealthResponse, ReadinessResponse, run_readiness_checks


def _resources(request: Request) -> Resources:
    resources: Resources = request.app.state.resources
    return resources


ResourcesDep = Annotated[Resources, Depends(_resources)]


def _service(resources: ResourcesDep) -> GatewayService:
    return resources.service


ServiceDep = Annotated[GatewayService, Depends(_service)]


def require_service_token(
    resources: ResourcesDep, authorization: Annotated[str | None, Header()] = None
) -> None:
    expected = resources.settings.gateway_service_token.get_secret_value()
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.encode(), expected.encode()):
        raise UnauthorizedError("Invalid service token", headers={"WWW-Authenticate": "Bearer"})


def get_caller(
    request: Request,
    x_tenant_id: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
) -> Caller:
    try:
        tenant_id = str(uuid.UUID(x_tenant_id or ""))
        user_id = str(uuid.UUID(x_user_id)) if x_user_id else None
    except ValueError as exc:
        raise InvalidRequestError(f"{h.TENANT_ID} (and {h.USER_ID}, if set) must be UUIDs") from exc
    return Caller(
        tenant_id=tenant_id,
        user_id=user_id,
        request_id=getattr(request.state, "request_id", None),
    )


CallerDep = Annotated[Caller, Depends(get_caller)]


def _use_cache(cache_control: str | None) -> bool:
    return "no-cache" not in (cache_control or "").lower()


# ---------- health ----------
health_router = APIRouter(tags=["health"])


@health_router.get("/healthz", response_model=HealthResponse)
async def healthz(resources: ResourcesDep) -> HealthResponse:
    s = resources.settings
    return HealthResponse(service=s.app_name, environment=s.environment)


@health_router.get(
    "/readyz", response_model=ReadinessResponse, responses={503: {"model": ReadinessResponse}}
)
async def readyz(resources: ResourcesDep, response: Response) -> ReadinessResponse:
    async def database() -> None:
        async with resources.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def redis() -> None:
        await resources.redis.ping()

    result = await run_readiness_checks(
        {"database": database, "redis": redis, "litellm": resources.upstream.ping},
        timeout=resources.settings.readiness_timeout_seconds,
    )
    if result.status != "ready":
        response.status_code = 503
    return result


# ---------- OpenAI-compatible API ----------
router = APIRouter(dependencies=[Depends(require_service_token)])


@router.post("/v1/chat/completions", response_model=ChatCompletion, tags=["llm"])
async def chat_completions(
    body: ChatCompletionRequest,
    service: ServiceDep,
    caller: CallerDep,
    response: Response,
    cache_control: Annotated[str | None, Header()] = None,
) -> Any:
    if body.stream:
        result = await service.chat_stream(body, caller, use_cache=_use_cache(cache_control))
        return StreamingResponse(
            result.body,
            media_type="text/event-stream",
            headers={
                **result.meta.to_headers(),
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    chat = await service.chat(body, caller, use_cache=_use_cache(cache_control))
    response.headers.update(chat.meta.to_headers())
    return chat.completion


@router.post("/v1/embeddings", tags=["llm"])
async def embeddings(
    body: EmbeddingRequest, service: ServiceDep, caller: CallerDep, response: Response
) -> Any:
    result, meta = await service.embed(body, caller)
    response.headers.update(meta.to_headers())
    return result


@router.get("/v1/models", tags=["llm"])
async def list_models(service: ServiceDep) -> dict[str, Any]:
    routes = service.routes
    models = [
        ModelInfo(id="auto", kind="chat", description="Picks chat-fast or chat-smart per request")
    ] + [
        ModelInfo(id=a.name, kind=a.kind, description=a.description, deployments=a.deployments)
        for a in routes.aliases.values()
    ]
    return {"object": "list", "data": [m.model_dump() for m in models]}


@router.get("/admin/providers", tags=["admin"])
async def providers(service: ServiceDep) -> dict[str, Any]:
    out = []
    for provider, deployments in sorted(service.routes.providers.items()):
        out.append(
            {
                "provider": provider,
                "configured": any(d.configured for d in deployments),
                "breaker": await service.breaker.snapshot(provider),
                "deployments": [
                    {
                        "name": d.name,
                        "kind": d.kind,
                        "configured": d.configured,
                        "rpm": d.rpm,
                        "context_window": d.context_window,
                    }
                    for d in deployments
                ],
            }
        )
    return {"providers": out}


@router.get("/admin/quota/{tenant_id}", tags=["admin"])
async def tenant_quota(tenant_id: uuid.UUID, service: ServiceDep) -> dict[str, Any]:
    tid = str(tenant_id)
    limit = await service.quota.limit(tid)
    return {
        "tenant_id": tid,
        "used_today": await service.quota.usage(tid),
        "daily_limit": limit if limit > 0 else None,
    }
