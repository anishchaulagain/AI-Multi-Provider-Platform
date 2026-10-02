import asyncio
import json
import random
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel

from llm_client import headers as h
from llm_client.types import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatMessage,
    EmbeddingRequest,
    EmbeddingResponse,
    ModelInfo,
)

_RETRYABLE_STATUS = {502, 503, 504}


class GatewayError(Exception):
    """Non-2xx response from the gateway, parsed from the platform error schema."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        self.headers = dict(headers or {})

    @classmethod
    def from_response(
        cls, status_code: int, body: bytes, headers: Mapping[str, str]
    ) -> "GatewayError":
        try:
            err = json.loads(body).get("error", {})
        except (ValueError, AttributeError):
            err = {}
        return cls(
            status_code,
            err.get("code", "gateway_error"),
            err.get("message", body.decode(errors="replace")[:200] or "Gateway error"),
            request_id=err.get("request_id"),
            headers=headers,
        )


class GatewayUnavailableError(GatewayError):
    """The gateway could not be reached at all."""


@dataclass(frozen=True)
class GatewayResult[T]:
    data: T
    meta: h.GatewayMeta


@dataclass
class Caller:
    """Identity forwarded to the gateway for quotas, usage and logs."""

    tenant_id: uuid.UUID | str
    user_id: uuid.UUID | str | None = None
    request_id: str | None = None

    def headers(self) -> dict[str, str]:
        out = {h.TENANT_ID: str(self.tenant_id)}
        if self.user_id:
            out[h.USER_ID] = str(self.user_id)
        if self.request_id:
            out[h.REQUEST_ID] = self.request_id
        return out


class GatewayStream:
    """An open streaming response. Iterate parsed chunks or raw SSE bytes."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.status_code = response.status_code
        self.headers = dict(response.headers)
        self.meta = h.GatewayMeta.from_headers(response.headers)

    def aiter_raw(self) -> AsyncIterator[bytes]:
        return self._response.aiter_raw()

    async def __aiter__(self) -> AsyncIterator[ChatCompletionChunk]:
        async for line in self._response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            data = json.loads(payload)
            if "error" in data:
                err = data["error"]
                raise GatewayError(
                    502, err.get("code", "stream_error"), err.get("message", "Stream failed")
                )
            yield ChatCompletionChunk.model_validate(data)


class GatewayClient:
    """Async client for the AI Gateway (OpenAI-compatible + platform headers).

    Non-streaming calls retry transient failures (connection errors, 502/503/504)
    with exponential backoff and jitter. The gateway itself already falls back
    across providers, so retries here only cover the gateway being briefly unavailable.
    """

    def __init__(
        self,
        base_url: str,
        service_token: str,
        *,
        timeout: float = 120.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=httpx.Timeout(timeout, connect=5.0),
            transport=transport,
        )
        self._max_retries = max_retries

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "GatewayClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------- low level ----------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        caller: Caller | None,
        json_body: Any = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        req_headers = {**(caller.headers() if caller else {}), **(extra_headers or {})}
        attempt = 0
        while True:
            try:
                response = await self._http.request(
                    method, path, json=json_body, headers=req_headers
                )
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise GatewayUnavailableError(
                        503, "gateway_unavailable", f"Gateway unreachable: {exc!r}"
                    ) from exc
            else:
                if response.status_code not in _RETRYABLE_STATUS or attempt >= self._max_retries:
                    if response.is_error:
                        raise GatewayError.from_response(
                            response.status_code, response.content, response.headers
                        )
                    return response
            await asyncio.sleep(0.25 * 2**attempt + random.uniform(0, 0.25))
            attempt += 1

    # ---------- chat ----------
    async def chat(
        self,
        request: ChatCompletionRequest,
        *,
        caller: Caller,
        no_cache: bool = False,
    ) -> GatewayResult[ChatCompletion]:
        body = request.model_copy(update={"stream": False}).model_dump(exclude_none=True)
        response = await self._request(
            "POST",
            "/v1/chat/completions",
            caller=caller,
            json_body=body,
            extra_headers={h.CACHE_CONTROL: "no-cache"} if no_cache else None,
        )
        return GatewayResult(
            ChatCompletion.model_validate(response.json()),
            h.GatewayMeta.from_headers(response.headers),
        )

    @asynccontextmanager
    async def stream(
        self,
        request: ChatCompletionRequest | Mapping[str, Any],
        *,
        caller: Caller,
        extra_headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[GatewayStream]:
        """Open a streaming completion. Raises GatewayError before yielding on failure."""
        body = (
            request.model_dump(exclude_none=True)
            if isinstance(request, ChatCompletionRequest)
            else dict(request)
        )
        body["stream"] = True
        req = self._http.build_request(
            "POST",
            "/v1/chat/completions",
            json=body,
            headers={**caller.headers(), **(extra_headers or {})},
        )
        try:
            response = await self._http.send(req, stream=True)
        except httpx.TransportError as exc:
            raise GatewayUnavailableError(
                503, "gateway_unavailable", f"Gateway unreachable: {exc!r}"
            ) from exc
        try:
            if response.is_error:
                await response.aread()
                raise GatewayError.from_response(
                    response.status_code, response.content, response.headers
                )
            yield GatewayStream(response)
        finally:
            await response.aclose()

    # ---------- embeddings ----------
    async def embed(
        self, inputs: str | list[str], *, caller: Caller, model: str = "embed"
    ) -> GatewayResult[EmbeddingResponse]:
        response = await self._request(
            "POST",
            "/v1/embeddings",
            caller=caller,
            json_body=EmbeddingRequest(model=model, input=inputs).model_dump(),
        )
        return GatewayResult(
            EmbeddingResponse.model_validate(response.json()),
            h.GatewayMeta.from_headers(response.headers),
        )

    # ---------- structured judging ----------
    async def judge[M: BaseModel](
        self,
        messages: list[ChatMessage],
        schema: type[M],
        *,
        caller: Caller,
        model: str = "judge",
    ) -> GatewayResult[M]:
        """Ask the judge alias for a JSON answer and validate it against `schema`."""
        instruction = ChatMessage(
            role="system",
            content=(
                "Respond with a single JSON object matching this JSON schema, and nothing else:\n"
                + json.dumps(schema.model_json_schema())
            ),
        )
        result = await self.chat(
            ChatCompletionRequest(
                model=model,
                messages=[instruction, *messages],
                temperature=0,
                response_format={"type": "json_object"},
            ),
            caller=caller,
            no_cache=True,
        )
        return GatewayResult(
            schema.model_validate_json(_strip_fences(result.data.text())), result.meta
        )

    # ---------- models & admin ----------
    async def models(self) -> list[ModelInfo]:
        response = await self._request("GET", "/v1/models", caller=None)
        return [ModelInfo.model_validate(m) for m in response.json()["data"]]

    async def providers(self) -> dict[str, Any]:
        response = await self._request("GET", "/admin/providers", caller=None)
        data: dict[str, Any] = response.json()
        return data

    async def tenant_quota(self, tenant_id: uuid.UUID | str) -> dict[str, Any]:
        response = await self._request("GET", f"/admin/quota/{tenant_id}", caller=None)
        data: dict[str, Any] = response.json()
        return data


def _strip_fences(text: str) -> str:
    """Some free models wrap JSON in ```json fences despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()
