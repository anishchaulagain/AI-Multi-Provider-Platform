"""Request orchestration: alias -> guardrails -> quota -> cache -> deployment chain."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from gateway.breaker import CircuitBreaker
from gateway.cache import CacheScope, ResponseCache, is_cacheable, params_hash, prompt_text
from gateway.config import GatewaySettings
from gateway.guardrails import Guardrail, run_guardrails
from gateway.quota import QuotaService
from gateway.routing import Deployment, Kind, RouteTable
from gateway.selection import choose_alias
from gateway.upstream import LiteLLMClient, UpstreamError
from gateway.usage import UsageEvent, UsageRecorder
from llm_client.headers import GatewayMeta
from llm_client.types import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionRequest,
    EmbeddingRequest,
    EmbeddingResponse,
    Usage,
)
from platform_core.errors import AppError, NotFoundError, ServiceUnavailableError
from platform_core.rate_limit import RateLimiter

logger = structlog.get_logger(__name__)

_EMBED_MAX_CHARS = 8000


@dataclass(frozen=True)
class Caller:
    tenant_id: str
    user_id: str | None = None
    request_id: str | None = None


@dataclass
class Attempt:
    deployment: str
    provider: str
    outcome: str  # skipped:missing_key | skipped:breaker_open | skipped:rate_limited | error
    status_code: int | None = None
    error: str | None = None


class InvalidRequestError(AppError):
    code = "invalid_request"


class AllDeploymentsFailedError(ServiceUnavailableError):
    code = "all_deployments_failed"


@dataclass
class ChatResult:
    completion: ChatCompletion
    meta: GatewayMeta


@dataclass
class StreamResult:
    meta: GatewayMeta
    body: AsyncIterator[bytes]


@dataclass
class _Ctx:
    """Per-request bookkeeping that ends up in the usage log."""

    caller: Caller
    kind: Kind
    alias: str
    started: float = field(default_factory=time.perf_counter)
    attempts: list[Attempt] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    cache: str = "off"

    @property
    def latency_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    def meta(self, dep: Deployment | None) -> GatewayMeta:
        return GatewayMeta(
            alias=self.alias,
            deployment=dep.name if dep else None,
            provider=dep.provider if dep else None,
            cache=self.cache,
            fallbacks=len(self.attempts),
            guardrail=",".join(self.flags) or None,
        )


def _sse(data: dict[str, Any] | str) -> bytes:
    payload = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    return f"data: {payload}\n\n".encode()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


class GatewayService:
    def __init__(
        self,
        *,
        settings: GatewaySettings,
        routes: RouteTable,
        upstream: LiteLLMClient,
        breaker: CircuitBreaker,
        rpm_limiter: RateLimiter,
        cache: ResponseCache,
        quota: QuotaService,
        usage: UsageRecorder,
        guardrails: list[Guardrail],
    ) -> None:
        self.settings = settings
        self.routes = routes
        self._upstream = upstream
        self.breaker = breaker
        self._rpm = rpm_limiter
        self._cache = cache
        self.quota = quota
        self._usage = usage
        self._guardrails = guardrails

    # ---------- alias resolution ----------
    def resolve_alias(
        self, model: str, kind: Kind, request: ChatCompletionRequest | None = None
    ) -> str:
        if model == "auto" and kind == "chat" and request is not None:
            return choose_alias(request, smart_min_chars=self.settings.auto_smart_min_chars)
        alias = self.routes.aliases.get(model)
        if alias is None:
            raise NotFoundError(
                f"Unknown model alias '{model}'",
                details={"available": sorted(self.routes.aliases)},
            )
        if alias.kind != kind:
            raise InvalidRequestError(f"Alias '{model}' is a {alias.kind} model, not {kind}")
        return model

    # ---------- deployment chain ----------
    async def _admit(self, dep: Deployment, ctx: _Ctx) -> bool:
        """Decide whether to try this deployment; records the reason when skipped."""
        if not dep.configured:
            ctx.attempts.append(Attempt(dep.name, dep.provider, "skipped:missing_key"))
            return False
        if not await self.breaker.allow(dep.provider):
            ctx.attempts.append(Attempt(dep.name, dep.provider, "skipped:breaker_open"))
            return False
        if dep.rpm:
            hit = await self._rpm.hit(
                f"deployment:{dep.name}", capacity=dep.rpm, per_minute=dep.rpm
            )
            if not hit.allowed:
                ctx.attempts.append(Attempt(dep.name, dep.provider, "skipped:rate_limited"))
                return False
        return True

    async def _on_failure(self, dep: Deployment, exc: UpstreamError, ctx: _Ctx) -> None:
        if exc.trips_breaker:
            await self.breaker.record_failure(dep.provider)
        ctx.attempts.append(
            Attempt(dep.name, dep.provider, "error", status_code=exc.status_code, error=exc.message)
        )
        logger.warning(
            "deployment_failed",
            alias=ctx.alias,
            deployment=dep.name,
            status=exc.status_code,
            error=exc.message[:200],
        )

    def _exhausted(self, ctx: _Ctx) -> AllDeploymentsFailedError:
        return AllDeploymentsFailedError(
            f"No deployment for '{ctx.alias}' could serve the request",
            details=[a.__dict__ for a in ctx.attempts],
        )

    async def _record(
        self,
        ctx: _Ctx,
        *,
        dep: Deployment | None,
        status: str,
        usage: Usage | None = None,
        error: str | None = None,
    ) -> None:
        usage = usage or Usage()
        if status == "ok":
            await self.quota.consume(ctx.caller.tenant_id, usage.total_tokens)
        await self._usage.record(
            UsageEvent(
                tenant_id=ctx.caller.tenant_id,
                user_id=ctx.caller.user_id,
                request_id=ctx.caller.request_id,
                kind=ctx.kind,
                alias=ctx.alias,
                deployment=dep.name if dep else None,
                provider=dep.provider if dep else None,
                status=status,
                cache=ctx.cache,
                fallbacks=len(ctx.attempts),
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                latency_ms=ctx.latency_ms,
                error=error,
            )
        )

    # ---------- chat: shared preparation ----------
    async def _prepare_chat(
        self, request: ChatCompletionRequest, caller: Caller, use_cache: bool
    ) -> tuple[_Ctx, ChatCompletionRequest, ChatCompletion | None, "_CachePlan | None"]:
        alias = self.resolve_alias(request.model, "chat", request)
        ctx = _Ctx(caller=caller, kind="chat", alias=alias)

        outcome = run_guardrails(request, self._guardrails)
        ctx.flags = outcome.flags
        if outcome.blocked_reason:
            await self._record(ctx, dep=None, status="blocked", error=outcome.blocked_reason)
            raise InvalidRequestError(outcome.blocked_reason, details={"guardrails": ctx.flags})
        request = outcome.request

        await self.quota.check(caller.tenant_id)

        plan: _CachePlan | None = None
        if use_cache and self.settings.cache_enabled and is_cacheable(request):
            ctx.cache = "miss"
            plan = _CachePlan(
                scope=CacheScope(caller.tenant_id, alias, params_hash(request)),
                text=prompt_text(request),
            )
            hit = await self._cache.get_exact(plan.scope, plan.text)
            if hit is not None:
                ctx.cache = "exact"
                return ctx, request, hit, plan
            if self.settings.semantic_cache_enabled:
                plan.vector = await self._embed_for_cache(plan.text)
                if plan.vector is not None:
                    hit = await self._cache.get_semantic(plan.scope, plan.vector)
                    if hit is not None:
                        ctx.cache = "semantic"
                        return ctx, request, hit, plan
        return ctx, request, None, plan

    async def _embed_for_cache(self, text: str) -> list[float] | None:
        """Best-effort embedding for the semantic cache (no quota, no usage row)."""
        alias = self.settings.semantic_cache_embed_alias
        if alias not in self.routes.aliases:
            return None
        ctx = _Ctx(caller=Caller(tenant_id="internal"), kind="embedding", alias=alias)
        for dep in self.routes.chain(alias):
            if not await self._admit(dep, ctx):
                continue
            try:
                data = await self._upstream.post_json(
                    "/v1/embeddings", {"model": dep.name, "input": text[:_EMBED_MAX_CHARS]}
                )
            except UpstreamError as exc:
                await self._on_failure(dep, exc, ctx)
                continue
            await self.breaker.record_success(dep.provider)
            vectors = EmbeddingResponse.model_validate(data).data
            return vectors[0].embedding if vectors else None
        return None

    def _upstream_body(
        self, request: ChatCompletionRequest, dep: Deployment, stream: bool
    ) -> dict[str, Any]:
        body = request.model_dump(exclude_none=True)
        body["model"] = dep.name
        body["stream"] = stream
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    # ---------- chat: non-streaming ----------
    async def chat(
        self, request: ChatCompletionRequest, caller: Caller, *, use_cache: bool = True
    ) -> ChatResult:
        ctx, request, hit, plan = await self._prepare_chat(request, caller, use_cache)
        if hit is not None:
            await self._record(ctx, dep=None, status="cache_hit")
            return ChatResult(hit, ctx.meta(None))

        for dep in self.routes.chain(ctx.alias):
            if not await self._admit(dep, ctx):
                continue
            try:
                data = await self._upstream.post_json(
                    "/v1/chat/completions", self._upstream_body(request, dep, stream=False)
                )
            except UpstreamError as exc:
                if not exc.retryable:
                    await self._record(ctx, dep=dep, status="error", error=exc.message)
                    raise InvalidRequestError(
                        exc.message, details={"deployment": dep.name}
                    ) from exc
                await self._on_failure(dep, exc, ctx)
                continue

            await self.breaker.record_success(dep.provider)
            completion = ChatCompletion.model_validate(data)
            if completion.usage is None:
                completion.usage = Usage(
                    prompt_tokens=_estimate_tokens(prompt_text(request)),
                    completion_tokens=_estimate_tokens(completion.text()),
                )
                completion.usage.total_tokens = (
                    completion.usage.prompt_tokens + completion.usage.completion_tokens
                )
            if (
                plan
                and completion.choices
                and completion.choices[0].finish_reason in ("stop", None)
            ):
                await self._cache.put(plan.scope, plan.text, completion, plan.vector)
            await self._record(ctx, dep=dep, status="ok", usage=completion.usage)
            return ChatResult(completion, ctx.meta(dep))

        await self._record(ctx, dep=None, status="error", error="all deployments failed")
        raise self._exhausted(ctx)

    # ---------- chat: streaming ----------
    async def chat_stream(
        self, request: ChatCompletionRequest, caller: Caller, *, use_cache: bool = True
    ) -> StreamResult:
        ctx, request, hit, plan = await self._prepare_chat(request, caller, use_cache)
        if hit is not None:
            await self._record(ctx, dep=None, status="cache_hit")
            return StreamResult(ctx.meta(None), self._replay(hit))

        for dep in self.routes.chain(ctx.alias):
            if not await self._admit(dep, ctx):
                continue
            try:
                response = await self._upstream.open_stream(
                    "/v1/chat/completions", self._upstream_body(request, dep, stream=True)
                )
            except UpstreamError as exc:
                if not exc.retryable:
                    await self._record(ctx, dep=dep, status="error", error=exc.message)
                    raise InvalidRequestError(
                        exc.message, details={"deployment": dep.name}
                    ) from exc
                await self._on_failure(dep, exc, ctx)
                continue
            # Committed to this deployment: headers are sent, no more fallback.
            return StreamResult(ctx.meta(dep), self._relay(response, dep, ctx, request, plan))

        await self._record(ctx, dep=None, status="error", error="all deployments failed")
        raise self._exhausted(ctx)

    async def _relay(
        self,
        response: httpx.Response,
        dep: Deployment,
        ctx: _Ctx,
        request: ChatCompletionRequest,
        plan: "_CachePlan | None",
    ) -> AsyncIterator[bytes]:
        state = _StreamState()
        try:
            try:
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = ChatCompletionChunk.model_validate_json(payload)
                    except ValueError:
                        continue
                    state.observe(chunk)
                    yield _sse(payload)
            except httpx.HTTPError as exc:
                state.error = f"{type(exc).__name__}: {exc}"
                yield _sse({"error": {"code": "upstream_stream_error", "message": state.error}})
            finally:
                await response.aclose()
            yield _sse("[DONE]")
        finally:
            # Shielded so usage/quota are still recorded if the client disconnects.
            await asyncio.shield(self._finish_stream(state, dep, ctx, request, plan))

    async def _finish_stream(
        self,
        state: "_StreamState",
        dep: Deployment,
        ctx: _Ctx,
        request: ChatCompletionRequest,
        plan: "_CachePlan | None",
    ) -> None:
        text = "".join(state.parts)
        usage = state.usage or Usage(
            prompt_tokens=_estimate_tokens(prompt_text(request)),
            completion_tokens=_estimate_tokens(text),
        )
        if not usage.total_tokens:
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        if state.error is not None:
            await self.breaker.record_failure(dep.provider)
        else:
            await self.breaker.record_success(dep.provider)
            if plan and state.first is not None and state.finish_reason in ("stop", None):
                completion = ChatCompletion.model_validate(
                    {
                        "id": state.first.id,
                        "created": state.first.created,
                        "model": state.first.model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": state.finish_reason or "stop",
                            }
                        ],
                        "usage": usage.model_dump(),
                    }
                )
                await self._cache.put(plan.scope, plan.text, completion, plan.vector)
        await self._record(
            ctx,
            dep=dep,
            status="ok" if state.error is None else "error",
            usage=usage,
            error=state.error,
        )

    @staticmethod
    async def _replay(completion: ChatCompletion) -> AsyncIterator[bytes]:
        """Serve a cached completion as a (single-chunk) SSE stream."""
        base = {
            "id": completion.id,
            "object": "chat.completion.chunk",
            "created": completion.created,
            "model": completion.model,
        }
        yield _sse(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": completion.text()},
                        "finish_reason": None,
                    }
                ],
            }
        )
        yield _sse(
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": completion.usage.model_dump() if completion.usage else None,
            }
        )
        yield _sse("[DONE]")

    # ---------- embeddings ----------
    async def embed(
        self, request: EmbeddingRequest, caller: Caller
    ) -> tuple[EmbeddingResponse, GatewayMeta]:
        alias = self.resolve_alias(request.model, "embedding")
        ctx = _Ctx(caller=caller, kind="embedding", alias=alias)
        await self.quota.check(caller.tenant_id)

        for dep in self.routes.chain(alias):
            if not await self._admit(dep, ctx):
                continue
            try:
                data = await self._upstream.post_json(
                    "/v1/embeddings", {"model": dep.name, "input": request.input}
                )
            except UpstreamError as exc:
                if not exc.retryable:
                    await self._record(ctx, dep=dep, status="error", error=exc.message)
                    raise InvalidRequestError(
                        exc.message, details={"deployment": dep.name}
                    ) from exc
                await self._on_failure(dep, exc, ctx)
                continue
            await self.breaker.record_success(dep.provider)
            result = EmbeddingResponse.model_validate(data)
            if result.usage is None:
                inputs = [request.input] if isinstance(request.input, str) else request.input
                tokens = sum(_estimate_tokens(t) for t in inputs)
                result.usage = Usage(prompt_tokens=tokens, total_tokens=tokens)
            await self._record(ctx, dep=dep, status="ok", usage=result.usage)
            return result, ctx.meta(dep)

        await self._record(ctx, dep=None, status="error", error="all deployments failed")
        raise self._exhausted(ctx)


@dataclass
class _StreamState:
    parts: list[str] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str | None = None
    first: ChatCompletionChunk | None = None
    error: str | None = None

    def observe(self, chunk: ChatCompletionChunk) -> None:
        self.first = self.first or chunk
        for choice in chunk.choices:
            self.parts.append(choice.delta.content or "")
            self.finish_reason = choice.finish_reason or self.finish_reason
        self.usage = chunk.usage or self.usage


@dataclass
class _CachePlan:
    scope: CacheScope
    text: str
    vector: list[float] | None = None
