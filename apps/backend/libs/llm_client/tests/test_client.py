import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import BaseModel

from llm_client import (
    Caller,
    ChatCompletionRequest,
    ChatMessage,
    GatewayClient,
    GatewayError,
    GatewayUnavailableError,
)

CALLER = Caller(tenant_id="00000000-0000-0000-0000-000000000001", user_id=None)


def _completion(content: str) -> dict[str, object]:
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _client(
    handler: Callable[[httpx.Request], httpx.Response], max_retries: int = 2
) -> GatewayClient:
    return GatewayClient(
        "http://gw", "tok", transport=httpx.MockTransport(handler), max_retries=max_retries
    )


def _req() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="chat-fast", messages=[ChatMessage(role="user", content="hi")]
    )


async def test_chat_sends_auth_and_identity_and_parses_meta() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=_completion("ok"),
            headers={"X-Gateway-Deployment": "d1", "X-Gateway-Fallbacks": "2"},
        )

    async with _client(handler) as client:
        result = await client.chat(_req(), caller=CALLER, no_cache=True)

    assert result.data.text() == "ok"
    assert result.meta.deployment == "d1"
    assert result.meta.fallbacks == 2
    assert seen[0].headers["authorization"] == "Bearer tok"
    assert seen[0].headers["x-tenant-id"] == CALLER.tenant_id
    assert seen[0].headers["cache-control"] == "no-cache"
    assert json.loads(seen[0].content)["stream"] is False


async def test_retries_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("llm_client.client.asyncio.sleep", _no_sleep)
    responses = [httpx.Response(503), httpx.Response(200, json=_completion("finally"))]

    async with _client(lambda r: responses.pop(0)) as client:
        result = await client.chat(_req(), caller=CALLER)
    assert result.data.text() == "finally"


async def test_does_not_retry_client_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("llm_client.client.asyncio.sleep", _no_sleep)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, json={"error": {"code": "quota_exceeded", "message": "nope", "request_id": "r1"}}
        )

    async with _client(handler) as client:
        with pytest.raises(GatewayError) as exc_info:
            await client.chat(_req(), caller=CALLER)
    assert calls == 1
    assert (exc_info.value.status_code, exc_info.value.code, exc_info.value.request_id) == (
        429,
        "quota_exceeded",
        "r1",
    )


async def test_unreachable_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("llm_client.client.asyncio.sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async with _client(handler, max_retries=1) as client:
        with pytest.raises(GatewayUnavailableError):
            await client.chat(_req(), caller=CALLER)


async def test_stream_parses_chunks() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=sse.encode(), headers={"X-Gateway-Alias": "chat-fast"})

    async with _client(handler) as client:
        async with client.stream(_req(), caller=CALLER) as stream:
            assert stream.meta.alias == "chat-fast"
            text = "".join([c.choices[0].delta.content or "" async for c in stream])
    assert text == "ab"


async def test_stream_error_raised_before_yield() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, json={"error": {"code": "all_deployments_failed", "message": "x"}}
        )

    async with _client(handler) as client:
        with pytest.raises(GatewayError, match="all_deployments_failed"):
            async with client.stream(_req(), caller=CALLER):
                pass


class Verdict(BaseModel):
    score: float
    reason: str


async def test_judge_parses_json_even_in_fences() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "judge"
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200, json=_completion('```json\n{"score": 0.9, "reason": "good"}\n```')
        )

    async with _client(handler) as client:
        result = await client.judge(
            [ChatMessage(role="user", content="rate")], Verdict, caller=CALLER
        )
    assert result.data == Verdict(score=0.9, reason="good")


async def _no_sleep(_: float) -> None:
    return None
