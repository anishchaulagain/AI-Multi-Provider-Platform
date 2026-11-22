import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response, status

from platform_api.api.deps import AdminPrincipal, CurrentPrincipal, DbSession, ResourcesDep
from platform_api.schemas.evals import (
    EvalResultDetail,
    EvalResultPage,
    EvalSummary,
    EvaluateRequest,
    FeedbackCreate,
    FeedbackRead,
    Metric,
)
from platform_api.services import evals as evals_service
from platform_db.models import EvalSource

router = APIRouter(tags=["evals"])


# ---------- feedback (the answer's owner) ----------
@router.post("/messages/{message_id}/feedback", response_model=FeedbackRead)
async def set_feedback(
    message_id: uuid.UUID, body: FeedbackCreate, principal: CurrentPrincipal, session: DbSession
) -> FeedbackRead:
    return await evals_service.set_feedback(
        session,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        message_id=message_id,
        body=body,
    )


@router.delete("/messages/{message_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feedback(
    message_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession
) -> Response:
    await evals_service.delete_feedback(
        session, tenant_id=principal.tenant_id, user_id=principal.user_id, message_id=message_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- evaluation results (admins: answers from every user in the workspace) ----------
@router.get("/evals/summary", response_model=EvalSummary)
async def eval_summary(
    principal: AdminPrincipal,
    session: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> EvalSummary:
    return await evals_service.summary(session, tenant_id=principal.tenant_id, days=days)


@router.get("/evals/results", response_model=EvalResultPage)
async def eval_results(
    principal: AdminPrincipal,
    session: DbSession,
    metric: Metric | None = None,
    model: Annotated[str | None, Query(max_length=128)] = None,
    source: Literal["online", "offline", "manual"] | None = None,
    order: Literal["worst", "recent"] = "recent",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EvalResultPage:
    return await evals_service.list_results(
        session,
        tenant_id=principal.tenant_id,
        metric=metric,
        model=model,
        source=source,
        order=order,
        limit=limit,
        offset=offset,
    )


@router.get("/evals/results/{result_id}", response_model=EvalResultDetail)
async def eval_result(
    result_id: uuid.UUID, principal: AdminPrincipal, session: DbSession, resources: ResourcesDep
) -> EvalResultDetail:
    return await evals_service.get_result(
        session, resources, tenant_id=principal.tenant_id, result_id=result_id
    )


@router.post("/evals/messages/{message_id}", response_model=EvalResultDetail)
async def evaluate_message(
    message_id: uuid.UUID,
    request: Request,
    principal: AdminPrincipal,
    session: DbSession,
    resources: ResourcesDep,
    body: EvaluateRequest | None = None,
) -> EvalResultDetail:
    """Score an answer now with the LLM judge (replaces any earlier evaluation)."""
    body = body or EvaluateRequest()
    return await evals_service.evaluate_now(
        session,
        resources,
        tenant_id=principal.tenant_id,
        message_id=message_id,
        source=EvalSource(body.source),
        run_id=body.run_id,
        request_id=getattr(request.state, "request_id", None),
    )
