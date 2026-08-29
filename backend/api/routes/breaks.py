"""Break list/detail and HITL approve / reject / override. Investigate is step 6."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.api import crud
from backend.api.break_investigate_jobs import (
    BreakInvestigateJob,
    get_job,
    schedule_break_investigate,
)
from backend.api.deps import get_db, resolve_actor
from backend.api.models import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from backend.api.schemas import (
    ApprovalRequest,
    ApprovalResponse,
    BreakDetailResponse,
    BreakInvestigateAccepted,
    BreakInvestigateJobOut,
    BreakListItem,
    InvestigateRequest,
    OverrideRequest,
    PaginatedBreaks,
)
from backend.api.services import (
    approve_break,
    build_break_detail,
    override_break,
    pick_actor,
    reject_break,
)

router = APIRouter(tags=["breaks"])


@router.get("/breaks", response_model=PaginatedBreaks)
def list_breaks(
    db: Session = Depends(get_db),
    desk: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    break_type: str | None = Query(default=None),
    date: date | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    status: str | None = Query(default=None),
    sort: crud.BreakSortField = Query(default="trade_date"),
    order: crud.SortOrder = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> PaginatedBreaks:
    start = from_date or date_from
    end = to_date or date_to
    try:
        start, end = crud.resolve_date_range(
            from_date=start, to_date=end, trade_date=date
        )
        status = crud.parse_break_status_filter(status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    items, total = crud.list_breaks(
        db,
        desk=desk,
        symbol=symbol,
        break_type=break_type,
        trade_date=None,
        date_from=start,
        date_to=end,
        status=status,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
    )
    type_options, others_types = crud.display_type_catalog(
        db,
        desk=desk,
        symbol=symbol,
        date_from=start,
        date_to=end,
        status=status,
    )
    audits = crud.latest_audits_by_break(db, [row.break_id for row in items])
    payload = [
        crud.apply_latest_audit(crud.break_to_list_item(row), audits.get(row.break_id))
        for row in items
    ]
    return PaginatedBreaks(
        items=[BreakListItem.model_validate(item) for item in payload],
        total=total,
        page=page,
        page_size=page_size,
        break_type_options=type_options,
        others_break_types=others_types,
    )


@router.get("/breaks/{break_id}", response_model=BreakDetailResponse)
def get_break_detail(
    break_id: UUID, db: Session = Depends(get_db)
) -> BreakDetailResponse:
    row = crud.get_break(db, break_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Break not found")
    return build_break_detail(db, row)


def _job_out(job: BreakInvestigateJob) -> BreakInvestigateJobOut:
    return BreakInvestigateJobOut(
        job_id=job.job_id,
        break_id=job.break_id,
        status=job.status,
        message=job.message,
        reply=job.reply,
        error=job.error,
        suggestion=job.suggestion,
    )


@router.post("/breaks/{break_id}/investigate", response_model=BreakInvestigateAccepted)
def investigate_break_endpoint(
    break_id: UUID,
    body: InvestigateRequest | None = None,
    db: Session = Depends(get_db),
) -> BreakInvestigateAccepted:
    """Queue agent investigation (max 5 tool calls). Does not auto-approve.

    Returns a job id immediately so CloudFront does not wait on Bedrock.
    """
    payload = body or InvestigateRequest()
    brk = crud.get_break(db, break_id)
    if brk is None:
        raise HTTPException(status_code=404, detail="Break not found")
    note = (payload.message or "").strip()
    job = schedule_break_investigate(
        brk=brk,
        message=note,
        provider=payload.provider,
        tools_enabled=payload.tools_enabled,
        session=db,
    )
    return BreakInvestigateAccepted(
        job_id=job.job_id,
        break_id=job.break_id,
        status=job.status,
        message=job.message,
    )


@router.get(
    "/breaks/{break_id}/investigate-jobs/{job_id}",
    response_model=BreakInvestigateJobOut,
)
def get_break_investigate_job(
    break_id: UUID,
    job_id: str,
) -> BreakInvestigateJobOut:
    """Poll a per-break investigate job (no LLM call)."""
    job = get_job(job_id)
    if job is None or job.break_id != break_id:
        raise HTTPException(status_code=404, detail="Investigate job not found")
    return _job_out(job)


@router.post("/breaks/{break_id}/approve", response_model=ApprovalResponse)
def post_approve(
    break_id: UUID,
    body: ApprovalRequest | None = None,
    db: Session = Depends(get_db),
    actor: str = Depends(resolve_actor),
) -> ApprovalResponse:
    payload = body or ApprovalRequest()
    return approve_break(
        db, break_id, actor=pick_actor(payload.actor, actor), note=payload.note
    )


@router.post("/breaks/{break_id}/reject", response_model=ApprovalResponse)
def post_reject(
    break_id: UUID,
    body: OverrideRequest,
    db: Session = Depends(get_db),
    actor: str = Depends(resolve_actor),
) -> ApprovalResponse:
    return reject_break(
        db, break_id, actor=pick_actor(body.actor, actor), note=body.note
    )


@router.post("/breaks/{break_id}/override", response_model=ApprovalResponse)
def post_override(
    break_id: UUID,
    body: OverrideRequest,
    db: Session = Depends(get_db),
    actor: str = Depends(resolve_actor),
) -> ApprovalResponse:
    return override_break(
        db, break_id, actor=pick_actor(body.actor, actor), note=body.note
    )
