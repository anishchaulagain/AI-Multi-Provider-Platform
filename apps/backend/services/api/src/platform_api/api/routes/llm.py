"""Public, OpenAI-compatible LLM endpoints.

Authenticates the caller (JWT or API key), then forwards to the internal gateway
with the caller's tenant/user identity. Works with the OpenAI SDKs:
    OpenAI(base_url="http://localhost:8000/api/v1/llm", api_key="aip_...")
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import StreamingResponse

from llm_client import Caller, ChatCompletionRequest, EmbeddingRequest, GatewayError
from llm_client import headers as gh
from platform_api.api.deps import CurrentPrincipal, ResourcesDep
from platform_core.errors import AppError

router = APIRouter(prefix="/llm", tags=["llm"])


def to_app_error(exc: GatewayError) -> AppError:
    """Surface gateway errors to clients with the same status and code."""
    err = AppError(exc.message)
    err.status_code = exc.status_code
    err.code = exc.code
    retry_after = exc.headers.get("retry-after")
    err.headers = {"Retry-After": retry_after} if retry_after else None
    return err


def _caller(principal: CurrentPrincipal, request: Request) -> Caller:
    return Caller(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        request_id=getattr(request.state, "request_id", None),
    )


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    principal: CurrentPrincipal,
    resources: ResourcesDep,
    request: Request,
    response: Response,
    cache_control: Annotated[str | None, Header()] = None,
) -> Any:
    gateway = resources.gateway
    caller = _caller(principal, request)
    passthrough = {gh.CACHE_CONTROL: cache_control} if cache_control else None

    if not body.stream:
        try:
            result = await gateway.chat(
                body, caller=caller, no_cache="no-cache" in (cache_control or "").lower()
            )
        except GatewayError as exc:
            raise to_app_error(exc) from exc
        response.headers.update(result.meta.to_headers())
        return result.data.model_dump(exclude_none=True)

    # Open the upstream stream *before* responding, so failures (quota, all
    # providers down) still produce a proper HTTP error instead of a broken stream.
    stack = AsyncExitStack()
    try:
        stream = await stack.enter_async_context(
            gateway.stream(body, caller=caller, extra_headers=passthrough)
        )
    except GatewayError as exc:
        await stack.aclose()
        raise to_app_error(exc) from exc

    async def relay() -> AsyncIterator[bytes]:
        try:
            async for raw in stream.aiter_raw():
                yield raw
        finally:
            await stack.aclose()

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={
            **stream.meta.to_headers(),
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/embeddings")
async def embeddings(
    body: EmbeddingRequest,
    principal: CurrentPrincipal,
    resources: ResourcesDep,
    request: Request,
    response: Response,
) -> Any:
    try:
        result = await resources.gateway.embed(
            body.input, caller=_caller(principal, request), model=body.model
        )
    except GatewayError as exc:
        raise to_app_error(exc) from exc
    response.headers.update(result.meta.to_headers())
    return result.data.model_dump(exclude_none=True)


@router.get("/models")
async def models(principal: CurrentPrincipal, resources: ResourcesDep) -> dict[str, Any]:
    try:
        data = await resources.gateway.models()
    except GatewayError as exc:
        raise to_app_error(exc) from exc
    return {"object": "list", "data": [m.model_dump() for m in data]}
