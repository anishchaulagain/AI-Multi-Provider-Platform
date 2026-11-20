"""Context windows per gateway alias, read from the gateway's model list and cached."""

import time

import structlog

from llm_client import GatewayClient, GatewayError, ModelInfo

logger = structlog.get_logger(__name__)


class ModelWindows:
    def __init__(self, gateway: GatewayClient, *, ttl_seconds: float = 300.0) -> None:
        self._gateway = gateway
        self._ttl = ttl_seconds
        self._models: dict[str, ModelInfo] = {}
        self._fetched = 0.0

    async def models(self) -> dict[str, ModelInfo]:
        if time.monotonic() - self._fetched > self._ttl or not self._models:
            try:
                self._models = {m.id: m for m in await self._gateway.models()}
                self._fetched = time.monotonic()
            except GatewayError as exc:
                logger.warning("model_list_unavailable", error=exc.code)
        return self._models

    async def window(self, alias: str, *, cap: int) -> int:
        """Budget window for `alias`: its context window, capped (unknown -> cap)."""
        info = (await self.models()).get(alias)
        window = info.context_window if info and info.context_window else cap
        return min(window, cap)

    async def chat_aliases(self) -> list[str]:
        return [m.id for m in (await self.models()).values() if m.kind == "chat"]
