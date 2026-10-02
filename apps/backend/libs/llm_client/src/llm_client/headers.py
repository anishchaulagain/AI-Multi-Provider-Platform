"""HTTP headers exchanged between callers and the gateway."""

from collections.abc import Mapping

from pydantic import BaseModel

# Request: caller identity. The gateway trusts these only with a valid service token.
TENANT_ID = "X-Tenant-ID"
USER_ID = "X-User-ID"
REQUEST_ID = "X-Request-ID"
# Request: set to "no-cache" to bypass the response cache.
CACHE_CONTROL = "Cache-Control"

# Response: how the request was served.
ALIAS = "X-Gateway-Alias"
DEPLOYMENT = "X-Gateway-Deployment"
PROVIDER = "X-Gateway-Provider"
CACHE = "X-Gateway-Cache"
FALLBACKS = "X-Gateway-Fallbacks"
GUARDRAIL = "X-Gateway-Guardrail"

META_HEADERS = (ALIAS, DEPLOYMENT, PROVIDER, CACHE, FALLBACKS, GUARDRAIL)


class GatewayMeta(BaseModel):
    alias: str | None = None
    deployment: str | None = None
    provider: str | None = None
    cache: str | None = None
    fallbacks: int = 0
    guardrail: str | None = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> "GatewayMeta":
        lower = {k.lower(): v for k, v in headers.items()}
        return cls(
            alias=lower.get(ALIAS.lower()),
            deployment=lower.get(DEPLOYMENT.lower()),
            provider=lower.get(PROVIDER.lower()),
            cache=lower.get(CACHE.lower()),
            fallbacks=int(lower.get(FALLBACKS.lower(), "0") or 0),
            guardrail=lower.get(GUARDRAIL.lower()),
        )

    def to_headers(self) -> dict[str, str]:
        values = {
            ALIAS: self.alias,
            DEPLOYMENT: self.deployment,
            PROVIDER: self.provider,
            CACHE: self.cache,
            FALLBACKS: str(self.fallbacks),
            GUARDRAIL: self.guardrail,
        }
        return {k: v for k, v in values.items() if v is not None}
