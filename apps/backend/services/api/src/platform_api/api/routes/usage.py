from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from llm_client import GatewayError
from platform_api.api.deps import AdminPrincipal, CurrentPrincipal, DbSession, ResourcesDep
from platform_api.api.routes.llm import to_app_error
from platform_api.schemas.usage import AliasUsage, UsageSummary
from platform_db.models import LlmUsage

router = APIRouter(tags=["usage"])


@router.get("/usage/summary", response_model=UsageSummary)
async def usage_summary(
    principal: CurrentPrincipal,
    session: DbSession,
    resources: ResourcesDep,
    hours: int = Query(default=24, ge=1, le=24 * 30),
) -> UsageSummary:
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = (
        await session.execute(
            select(
                LlmUsage.alias,
                func.count(),
                func.coalesce(func.sum(LlmUsage.total_tokens), 0),
                func.count().filter(LlmUsage.status == "cache_hit"),
                func.count().filter(LlmUsage.status == "error"),
                func.coalesce(func.avg(LlmUsage.latency_ms), 0),
            )
            .where(LlmUsage.tenant_id == principal.tenant_id, LlmUsage.created_at >= since)
            .group_by(LlmUsage.alias)
            .order_by(LlmUsage.alias)
        )
    ).all()
    by_alias = [
        AliasUsage(
            alias=alias,
            requests=requests,
            total_tokens=int(tokens),
            cache_hits=hits,
            errors=errors,
            avg_latency_ms=round(float(latency)),
        )
        for alias, requests, tokens, hits, errors, latency in rows
    ]

    quota: dict[str, Any] = {}
    try:
        quota = await resources.gateway.tenant_quota(principal.tenant_id)
    except GatewayError:
        pass  # gateway down: still show historical usage

    return UsageSummary(
        hours=hours,
        requests=sum(a.requests for a in by_alias),
        total_tokens=sum(a.total_tokens for a in by_alias),
        cache_hits=sum(a.cache_hits for a in by_alias),
        errors=sum(a.errors for a in by_alias),
        by_alias=by_alias,
        tokens_used_today=quota.get("used_today"),
        daily_token_limit=quota.get("daily_limit"),
    )


@router.get("/admin/providers")
async def providers(_: AdminPrincipal, resources: ResourcesDep) -> dict[str, Any]:
    try:
        return await resources.gateway.providers()
    except GatewayError as exc:
        raise to_app_error(exc) from exc
