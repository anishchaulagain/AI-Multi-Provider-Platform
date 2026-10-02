from pydantic import BaseModel


class AliasUsage(BaseModel):
    alias: str
    requests: int
    total_tokens: int
    cache_hits: int
    errors: int
    avg_latency_ms: int


class UsageSummary(BaseModel):
    hours: int
    requests: int
    total_tokens: int
    cache_hits: int
    errors: int
    by_alias: list[AliasUsage]
    # From the gateway's live quota counters (None if the gateway is unreachable).
    tokens_used_today: int | None = None
    daily_token_limit: int | None = None
