import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

Check = Callable[[], Awaitable[object]]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    environment: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, str]


async def _run(name: str, check: Check, timeout: float) -> tuple[str, str]:
    try:
        await asyncio.wait_for(check(), timeout=timeout)
        return name, "ok"
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        logger.warning("readiness_check_failed", check=name, error=repr(exc))
        return name, "error"


async def run_readiness_checks(
    checks: dict[str, Check],
    *,
    timeout: float,
    optional: dict[str, Check] | None = None,
) -> ReadinessResponse:
    """Run all checks concurrently. Ready only if every required check passes;
    optional checks are reported but don't affect the status."""
    optional = optional or {}
    everything = {**checks, **optional}
    results = dict(await asyncio.gather(*(_run(n, c, timeout) for n, c in everything.items())))
    ready = all(results[name] == "ok" for name in checks)
    return ReadinessResponse(status="ready" if ready else "not_ready", checks=results)
