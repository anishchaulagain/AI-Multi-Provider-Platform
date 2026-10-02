"""Thin client for the LiteLLM proxy, plus error classification for fallback."""

from typing import Any

import httpx


class UpstreamError(Exception):
    """A deployment call failed.

    retryable    -> try the next deployment in the chain
    trips_breaker -> counts as a provider failure for the circuit breaker
    """

    def __init__(
        self, message: str, *, status_code: int | None, retryable: bool, trips_breaker: bool
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.trips_breaker = trips_breaker


def _error_message(response: httpx.Response) -> str:
    try:
        err = response.json().get("error", {})
        message = err.get("message") if isinstance(err, dict) else str(err)
    except ValueError:
        message = None
    return (message or response.text or response.reason_phrase)[:500]


def classify(response: httpx.Response) -> UpstreamError:
    status = response.status_code
    message = _error_message(response)
    if status in (400, 413, 422):
        # Bad request for this model (invalid params, context too long). The
        # client's fault, so don't penalise the provider or retry elsewhere.
        return UpstreamError(message, status_code=status, retryable=False, trips_breaker=False)
    if status == 404:
        # Model not available on this deployment (e.g. not pulled into Ollama).
        return UpstreamError(message, status_code=status, retryable=True, trips_breaker=False)
    # 401/403 (bad key), 429 (rate limit), 5xx, anything else: provider problem.
    return UpstreamError(message, status_code=status, retryable=True, trips_breaker=True)


def transport_error(exc: Exception) -> UpstreamError:
    return UpstreamError(
        f"{type(exc).__name__}: {exc}", status_code=None, retryable=True, trips_breaker=True
    )


class LiteLLMClient:
    def __init__(
        self,
        base_url: str,
        master_key: str,
        *,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {master_key}"},
            timeout=httpx.Timeout(timeout, connect=5.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        if response.is_error:
            raise classify(response)
        data: dict[str, Any] = response.json()
        return data

    async def open_stream(self, path: str, body: dict[str, Any]) -> httpx.Response:
        """Send a streaming request; returns the open response only if it succeeded."""
        request = self._http.build_request("POST", path, json=body)
        try:
            response = await self._http.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        if response.is_error:
            await response.aread()
            await response.aclose()
            raise classify(response)
        return response

    async def ping(self) -> None:
        response = await self._http.get("/health/liveliness", timeout=3.0)
        response.raise_for_status()
