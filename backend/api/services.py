"""Approval-gate and break-detail assembly. Never auto-approves."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.api import crud
from backend.api.auth import auth_is_required
from backend.api.models import (
    AUDIT_APPROVED,
    AUDIT_OVERRIDDEN,
    AUDIT_REJECTED,
    BREAK_STATUS_OVERRIDDEN,
    BREAK_STATUS_REJECTED,
    BREAK_STATUS_RESOLVED,
    HIGH_CONFIDENCE_THRESHOLD,
    LOW_NOTIONAL_THRESHOLD,
    REVIEW_MANUAL,
    REVIEW_ONE_CLICK,
    TERMINAL_BREAK_STATUSES,
)
from backend.api.schemas import (
    ApprovalResponse,
    AuditDecisionOut,
    BreakDetailResponse,
    NormalizedTradeOut,
    SideBySide,
    SuggestionOut,
)
from backend.db.models import (
    Break,
    RawBrokerTrade,
    RawDeskTrade,
    ResolutionSuggestion,
)
from backend.pipeline.book_fix import (
    BookFixError,
    NO_SUGGESTION_MESSAGE,
    apply_suggested_action,
    needs_suggestion_to_approve,
)


def review_routing(
    suggestion: ResolutionSuggestion | None,
    notional_at_risk: float,
) -> str:
    """Dashboard hint only. High confidence + low notional → one-click UI.

    The API never resolves a break without an explicit approve/override POST.
    """
    if suggestion is None:
        return REVIEW_MANUAL
    if (
        suggestion.confidence >= HIGH_CONFIDENCE_THRESHOLD
        and notional_at_risk <= LOW_NOTIONAL_THRESHOLD
    ):
        return REVIEW_ONE_CLICK
    return REVIEW_MANUAL


def suggestion_out(break_id: UUID, row: ResolutionSuggestion | None) -> SuggestionOut:
    """§6.3 shape; null fields when the agent has not written a row."""
    if row is None:
        return SuggestionOut(break_id=break_id, evidence=[])
    evidence = row.evidence if isinstance(row.evidence, list) else []
    return SuggestionOut(
        break_id=break_id,
        root_cause=row.root_cause,
        confidence=row.confidence,
        explanation=row.explanation,
        suggested_action=row.suggested_action,
        evidence=evidence,
        suggestion_id=row.suggestion_id,
        inferred=bool(getattr(row, "inferred", False)),
    )


def _raw_broker_dict(row: RawBrokerTrade) -> dict[str, Any]:
    return {
        "broker_trade_id": row.broker_trade_id,
        "symbol": row.symbol,
        "trade_date": row.trade_date.isoformat() if row.trade_date else None,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
        "settlement_date": row.settlement_date.isoformat()
        if row.settlement_date
        else None,
        "settlement_datetime": row.settlement_datetime.isoformat()
        if row.settlement_datetime
        else None,
        "side": row.side,
        "quantity": row.quantity,
        "price": row.price,
        "currency": row.currency,
        "account_id": row.account_id,
        "execution_venue": row.execution_venue,
        "pair_id": row.pair_id,
    }


def _raw_desk_dict(row: RawDeskTrade) -> dict[str, Any]:
    return {
        "blotter_id": row.blotter_id,
        "ticker": row.ticker,
        "trade_date": row.trade_date.isoformat() if row.trade_date else None,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
        "settle_date": row.settle_date.isoformat() if row.settle_date else None,
        "settlement_datetime": row.settlement_datetime.isoformat()
        if row.settlement_datetime
        else None,
        "side": row.side,
        "qty": row.qty,
        "px": row.px,
        "ccy": row.ccy,
        "desk_code": row.desk_code,
        "trader": row.trader,
        "pair_id": row.pair_id,
    }


def build_break_detail(session: Session, row: Break) -> BreakDetailResponse:
    broker_ids, desk_ids = crud.trade_ids_for_break(row)
    broker_norm = crud.get_normalized_by_ids(session, "broker", broker_ids)
    desk_norm = crud.get_normalized_by_ids(session, "desk", desk_ids)
    broker_raw = crud.get_raw_broker(session, broker_ids)
    desk_raw = crud.get_raw_desk(session, desk_ids)
    suggestion = crud.latest_suggestion(session, row.break_id)
    item = crud.break_to_list_item(row)
    decisions = [
        AuditDecisionOut.model_validate(crud.audit_to_decision(a))
        for a in crud.list_audits_for_break(session, row.break_id)
    ]
    return BreakDetailResponse(
        break_id=row.break_id,
        break_type=row.break_type,
        status=row.status,
        symbol=row.symbol,
        trade_date=row.trade_date,
        executed_at=item.get("executed_at"),
        pair_id=row.pair_id,
        desk=item["desk"],
        notional_at_risk=item["notional_at_risk"],
        detail=row.detail,
        cluster_id=row.cluster_id,
        created_at=row.created_at,
        broker_side=SideBySide(
            trade_ids=broker_ids,
            normalized=[NormalizedTradeOut.model_validate(t) for t in broker_norm],
            raw=[_raw_broker_dict(t) for t in broker_raw],
        ),
        desk_side=SideBySide(
            trade_ids=desk_ids,
            normalized=[NormalizedTradeOut.model_validate(t) for t in desk_norm],
            raw=[_raw_desk_dict(t) for t in desk_raw],
        ),
        suggestion=suggestion_out(row.break_id, suggestion),
        review_routing=review_routing(suggestion, item["notional_at_risk"]),
        decisions=decisions,
    )


def _require_break(session: Session, break_id: UUID) -> Break:
    row = crud.get_break(session, break_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Break not found")
    return row


def _require_open_for_decision(row: Break) -> None:
    if row.status in TERMINAL_BREAK_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Break is already {row.status}",
        )


def _write_decision_memory(
    session: Session,
    *,
    brk: Break,
    audit: Any,
    suggestion: ResolutionSuggestion | None,
) -> None:
    """HITL learning signal. Never skips audit_log; never fails the decision."""
    from backend.agent.memory_writer import record_human_decision_memory

    record_human_decision_memory(
        session, brk=brk, audit=audit, suggestion=suggestion
    )


def approve_break(
    session: Session,
    break_id: UUID,
    *,
    actor: str,
    note: str | None = None,
) -> ApprovalResponse:
    """Human approve: apply suggested_action to the books, then resolve + audit."""
    row = _require_break(session, break_id)
    _require_open_for_decision(row)
    suggestion = crud.latest_suggestion(session, break_id)
    if needs_suggestion_to_approve(row.break_type) and suggestion is None:
        raise HTTPException(status_code=400, detail=NO_SUGGESTION_MESSAGE)
    if suggestion is not None:
        try:
            apply_suggested_action(session, row, suggestion)
        except BookFixError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        row = _require_break(session, break_id)
    audit = crud.add_audit(
        session,
        break_id=break_id,
        actor=actor,
        action=AUDIT_APPROVED,
        suggestion=suggestion,
        override_note=note,
    )
    row.status = BREAK_STATUS_RESOLVED
    session.flush()
    _write_decision_memory(session, brk=row, audit=audit, suggestion=suggestion)
    return ApprovalResponse(
        break_id=row.break_id,
        status=row.status,
        action=AUDIT_APPROVED,
        audit_id=audit.audit_id,
        suggestion_id=suggestion.suggestion_id if suggestion else None,
    )


def reject_break(
    session: Session,
    break_id: UUID,
    *,
    actor: str,
    note: str,
) -> ApprovalResponse:
    """Reject the suggestion (or record a reject with no suggestion). Does not mutate trades."""
    row = _require_break(session, break_id)
    _require_open_for_decision(row)
    if not note or not note.strip():
        raise HTTPException(status_code=422, detail="note is required")
    suggestion = crud.latest_suggestion(session, break_id)
    audit = crud.add_audit(
        session,
        break_id=break_id,
        actor=actor,
        action=AUDIT_REJECTED,
        suggestion=suggestion,
        override_note=note.strip(),
    )
    row.status = BREAK_STATUS_REJECTED
    session.flush()
    _write_decision_memory(session, brk=row, audit=audit, suggestion=suggestion)
    return ApprovalResponse(
        break_id=row.break_id,
        status=row.status,
        action=AUDIT_REJECTED,
        audit_id=audit.audit_id,
        suggestion_id=suggestion.suggestion_id if suggestion else None,
    )


def override_break(
    session: Session,
    break_id: UUID,
    *,
    actor: str,
    note: str,
) -> ApprovalResponse:
    """Human override / resolve without accepting the agent. Always audits."""
    row = _require_break(session, break_id)
    _require_open_for_decision(row)
    if not note or not note.strip():
        raise HTTPException(status_code=422, detail="note is required")
    suggestion = crud.latest_suggestion(session, break_id)
    audit = crud.add_audit(
        session,
        break_id=break_id,
        actor=actor,
        action=AUDIT_OVERRIDDEN,
        suggestion=suggestion,
        override_note=note.strip(),
    )
    row.status = BREAK_STATUS_OVERRIDDEN
    session.flush()
    _write_decision_memory(session, brk=row, audit=audit, suggestion=suggestion)
    return ApprovalResponse(
        break_id=row.break_id,
        status=row.status,
        action=AUDIT_OVERRIDDEN,
        audit_id=audit.audit_id,
        suggestion_id=suggestion.suggestion_id if suggestion else None,
    )


def pick_actor(body_actor: str | None, header_actor: str) -> str:
    """Body actor is only honored when Cognito is off (local tests)."""
    if auth_is_required():
        return header_actor
    if body_actor and body_actor.strip():
        return body_actor.strip()
    return header_actor
