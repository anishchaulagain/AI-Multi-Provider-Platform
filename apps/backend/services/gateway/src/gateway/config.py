from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator

from platform_core.config import BaseServiceSettings

DEV_SERVICE_TOKEN = "dev-gateway-token-change-me"


class GatewaySettings(BaseServiceSettings):
    app_name: str = "AI Gateway"

    # Upstream LiteLLM proxy (provider translation only; routing lives here).
    litellm_url: str = "http://localhost:4000"
    litellm_master_key: SecretStr = SecretStr("sk-local-dev")
    upstream_timeout_seconds: float = 120.0

    # Callers authenticate with this shared service token.
    gateway_service_token: SecretStr = SecretStr(DEV_SERVICE_TOKEN)

    # Alias -> deployment routing table.
    routes_file: str = "../../infra/gateway/routes.yaml"

    # Circuit breaker (per provider)
    breaker_failure_threshold: int = 5
    breaker_window_seconds: int = 60
    breaker_cooldown_seconds: int = 30

    # Response cache: exact (Redis) + semantic (Qdrant)
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600
    semantic_cache_enabled: bool = True
    semantic_cache_threshold: float = 0.95
    semantic_cache_embed_alias: str = "embed"
    qdrant_url: str = "http://localhost:6333"

    # Quotas: tenants.daily_token_quota overrides this; <= 0 means unlimited.
    default_daily_token_quota: int = 2_000_000

    # Guardrails
    guardrail_injection_mode: Literal["off", "flag", "block"] = "flag"
    guardrail_redact_pii: bool = False

    # `model: "auto"` picks chat-smart above this prompt size (characters).
    auto_smart_min_chars: int = 6000

    @model_validator(mode="after")
    def _require_real_token_outside_local(self) -> "GatewaySettings":
        token = self.gateway_service_token.get_secret_value()
        if self.is_deployed and (token == DEV_SERVICE_TOKEN or len(token) < 32):
            raise ValueError(
                "GATEWAY_SERVICE_TOKEN must be a random value of at least 32 bytes "
                "outside local/test environments"
            )
        return self


@lru_cache
def get_settings() -> GatewaySettings:
    return GatewaySettings()
