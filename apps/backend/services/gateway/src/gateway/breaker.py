"""Per-provider circuit breaker stored in Redis (shared across gateway replicas).

closed     -> requests flow; failures are counted in a rolling window
open       -> `threshold` failures inside the window; provider skipped for `cooldown`
half_open  -> cooldown elapsed; a single probe request is let through.
              success closes the breaker, failure re-opens it immediately.
"""

from typing import Literal

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = structlog.get_logger(__name__)

State = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    def __init__(
        self, redis: Redis, *, threshold: int, window_seconds: int, cooldown_seconds: int
    ) -> None:
        self._redis = redis
        self._threshold = threshold
        self._window = window_seconds
        self._cooldown = cooldown_seconds

    @staticmethod
    def _keys(provider: str) -> tuple[str, str, str, str]:
        base = f"cb:{provider}"
        return f"{base}:failures", f"{base}:open", f"{base}:tripped", f"{base}:probe"

    async def state(self, provider: str) -> State:
        _, open_key, tripped_key, _ = self._keys(provider)
        try:
            is_open, tripped = (
                await self._redis.exists(open_key),
                await self._redis.exists(tripped_key),
            )
        except RedisError:
            return "closed"
        if is_open:
            return "open"
        return "half_open" if tripped else "closed"

    async def allow(self, provider: str) -> bool:
        state = await self.state(provider)
        if state == "closed":
            return True
        if state == "open":
            return False
        # half_open: only one concurrent probe.
        _, _, _, probe_key = self._keys(provider)
        try:
            return bool(await self._redis.set(probe_key, "1", nx=True, ex=self._cooldown))
        except RedisError:
            return True

    async def record_success(self, provider: str) -> None:
        try:
            await self._redis.delete(*self._keys(provider))
        except RedisError:
            pass

    async def record_failure(self, provider: str) -> None:
        failures_key, open_key, tripped_key, probe_key = self._keys(provider)
        try:
            if await self._redis.exists(tripped_key):
                # Failed while half-open (or a straggler while open): re-open immediately.
                await self._trip(provider)
                return
            failures = await self._redis.incr(failures_key)
            if failures == 1:
                await self._redis.expire(failures_key, self._window)
            if failures >= self._threshold:
                await self._trip(provider)
        except RedisError:
            logger.warning("breaker_unavailable", provider=provider)

    async def _trip(self, provider: str) -> None:
        failures_key, open_key, tripped_key, probe_key = self._keys(provider)
        pipe = self._redis.pipeline()
        pipe.set(open_key, "1", ex=self._cooldown)
        pipe.set(tripped_key, "1", ex=self._cooldown * 20)
        pipe.delete(failures_key, probe_key)
        await pipe.execute()
        logger.warning("breaker_opened", provider=provider, cooldown_s=self._cooldown)

    async def snapshot(self, provider: str) -> dict[str, object]:
        failures_key, open_key, _, _ = self._keys(provider)
        try:
            failures = int(await self._redis.get(failures_key) or 0)
            open_ttl = await self._redis.ttl(open_key)
        except RedisError:
            return {"state": "unknown", "recent_failures": None, "reopens_in_s": None}
        return {
            "state": await self.state(provider),
            "recent_failures": failures,
            "reopens_in_s": open_ttl if open_ttl > 0 else None,
        }
