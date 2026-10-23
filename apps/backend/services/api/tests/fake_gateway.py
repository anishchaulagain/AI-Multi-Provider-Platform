"""In-process stand-in for the AI Gateway (used via httpx.MockTransport)."""

import hashlib
import json
import math
import re
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


def embed(text: str, dim: int = 64) -> list[float]:
    """Bag-of-words vector: texts sharing words are similar (prefixes ignored)."""
    vec = [0.0] * dim
    for word in re.findall(r"\w+", text.lower().replace("search_query", "")):
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@dataclass
class FakeGateway:
    requests: list[httpx.Request] = field(default_factory=list)
    # Set to (status, error_code) to make chat/embedding calls fail.
    fail_with: tuple[int, str] | None = None
    unreachable: bool = False
    # Streamed when the prompt carries numbered sources (grounded chat).
    rag_answer: str = "Qdrant is written in Rust [1]. Invented [9]."
    rewrite_reply: str = "What is Qdrant written in?"
    rag_prompts: list[str] = field(default_factory=list)
    rewrites: int = 0

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
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return httpx.Response(
                200,
                json={
                    "data": [{"index": i, "embedding": embed(t)} for i, t in enumerate(inputs)],
                    "model": "e",
                },
                headers={**META, "X-Gateway-Alias": "embed"},
            )
        last = str(body["messages"][-1].get("content", ""))
        if body.get("stream") and last.startswith("Sources:"):
            self.rag_prompts.append(last)
            words = self.rag_answer.split(" ")
            chunks: list[dict[str, Any]] = [
                {
                    "choices": [
                        {"index": 0, "delta": {"content": w + (" " if i < len(words) - 1 else "")}}
                    ]
                }
                for i, w in enumerate(words)
            ]
            chunks.append(
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 9, "total_tokens": 129},
                }
            )
            sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(
                200,
                content=sse.encode(),
                headers={
                    **META,
                    "X-Gateway-Alias": "chat-smart",
                    "content-type": "text/event-stream",
                },
            )
        if not body.get("stream") and "standalone search question" in str(
            body["messages"][0].get("content", "")
        ):
            self.rewrites += 1
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": self.rewrite_reply}}]
                },
                headers=META,
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
