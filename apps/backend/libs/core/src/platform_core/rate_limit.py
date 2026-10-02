from dataclasses import dataclass

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = structlog.get_logger(__name__)

# Token bucket. Uses the Redis server clock so multiple API replicas agree.
# KEYS[1] = bucket key; ARGV[1] = capacity; ARGV[2] = refill rate (tokens/sec)
# Returns {allowed (0|1), remaining tokens (floored), retry_after seconds}
_TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

tokens = math.min(capacity, tokens + (math.max(0, now - ts) / 1000.0) * rate)

local allowed = 0
local retry_after = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry_after = math.ceil((1 - tokens) / rate)
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate * 1000) + 1000)
return {allowed, math.floor(tokens), retry_after}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after)
        return headers


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._script = redis.register_script(_TOKEN_BUCKET_LUA)

    async def hit(self, key: str, *, capacity: int, per_minute: int) -> RateLimitResult:
        try:
            allowed, remaining, retry_after = await self._script(
                keys=[f"ratelimit:{key}"], args=[capacity, per_minute / 60]
            )
        except RedisError:
            # Fail open: an unavailable Redis should not take the API down.
            logger.warning("rate_limiter_unavailable", key=key)
            return RateLimitResult(True, capacity, capacity, 0)
        return RateLimitResult(bool(allowed), capacity, int(remaining), int(retry_after))
