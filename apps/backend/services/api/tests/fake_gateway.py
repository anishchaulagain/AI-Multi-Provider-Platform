"""In-process stand-in for the AI Gateway (used via httpx.MockTransport)."""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

META = {
    "X-Gateway-Alias": "chat-fast",
    "X-Gateway-Deployment": "fake-deployment",
    "X-Gateway-Provider": "fake",
    "X-Gateway-Cache": "miss",
    "X-Gateway-Fallbacks": "0",
}


@dataclass
class FakeGateway:
    requests: list[httpx.Request] = field(default_factory=list)
    # Set to (status, error_code) to make chat/embedding calls fail.
    fail_with: tuple[int, str] | None = None
    unreachable: bool = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.unreachable:
            raise httpx.ConnectError("gateway down", request=request)
        path = request.url.path

        if path == "/v1/models":
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "chat-fast", "kind": "chat"}]},
            )
        if path == "/admin/providers":
            return httpx.Response(200, json={"providers": [{"provider": "fake"}]})
        if path.startswith("/admin/quota/"):
            return httpx.Response(200, json={"used_today": 42, "daily_limit": 1000})

        if self.fail_with:
            status, code = self.fail_with
            return httpx.Response(status, json={"error": {"code": code, "message": f"fake {code}"}})

        body: dict[str, Any] = json.loads(request.content)
        if path == "/v1/embeddings":
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.1, 0.2]}], "model": "e"},
                headers={**META, "X-Gateway-Alias": "embed"},
            )
        if body.get("stream"):
            sse = (
                'data: {"choices":[{"index":0,"delta":{"content":"Hi"}}]}\n\n'
                'data: {"choices":[{"index":0,"delta":{"content":" there"}}]}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(
                200, content=sse.encode(), headers={**META, "content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={
                "id": "x",
                "model": "fake-deployment",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hi there"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
            headers=META,
        )

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]
