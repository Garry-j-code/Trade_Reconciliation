"""Parameterized ORM queries — no free-form SQL from clients."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Float, Select, func, select
from sqlalchemy.orm import Session

from backend.db.models import (
    AuditLog,
    Break,
    Match,
    NormalizedTrade,
    RawBrokerTrade,
    RawDeskTrade,
    ResolutionSuggestion,
)
from backend.pipeline.rules import parse_trade_ids


def ping_db(session: Session) -> bool:
    """Return True when ``SELECT 1`` succeeds."""
    session.execute(select(1))
    return True


def _break_desk(row: Break) -> str | None:
    detail = row.detail or {}
    desk = detail.get("desk")
    return str(desk) if desk else None


def _break_notional(row: Break) -> float:
    detail = row.detail or {}
    raw = detail.get("notional_at_risk")
    try:
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def break_to_list_item(row: Break) -> dict[str, Any]:
    """Serialize a break row plus denormalized desk / notional from detail."""
    return {
        "break_id": row.break_id,
        "break_type": row.break_type,
        "status": row.status,
        "symbol": row.symbol,
        "trade_date": row.trade_date,
        "pair_id": row.pair_id,
        "desk": _break_desk(row),
        "notional_at_risk": _break_notional(row),
        "created_at": row.created_at,
    }


BreakSortField = Literal[
    "break_type", "status", "desk", "symbol", "trade_date", "notional"
]
SortOrder = Literal["asc", "desc"]

BREAK_SORT_FIELDS: tuple[str, ...] = (
    "break_type",
    "status",
    "desk",
    "symbol",
    "trade_date",
    "notional",
)


def pair_based_summary(
    *,
    pair_count: int,
    broker_leg_count: int,
    desk_leg_count: int,
    matched_pair_count: int,
    match_row_count: int,
    break_count: int,
    open_break_count: int,
    breaks_by_type: list[dict[str, Any]],
    notional_at_risk: float,
) -> dict[str, Any]:
    """Client-facing metrics: unique economic pairs, not legs or match rows.

    ``total_trades`` / ``match_count`` keep existing field names but mean
    unique ``pair_id`` counts. Split-fill clusters produce many match rows
    for one pair; those rows must not inflate the clean-match rate.
    """
    pct = (100.0 * matched_pair_count / pair_count) if pair_count else 0.0
    return {
        "total_trades": pair_count,
        "pair_count": pair_count,
        "broker_leg_count": broker_leg_count,
        "desk_leg_count": desk_leg_count,
        "match_count": matched_pair_count,
        "matched_pair_count": matched_pair_count,
        "match_row_count": match_row_count,
        "break_count": break_count,
        "open_break_count": open_break_count,
        "pct_clean_matched": round(pct, 4),
        "breaks_by_type": breaks_by_type,
        "notional_at_risk": round(notional_at_risk, 4),
    }


def summary_stats(session: Session) -> dict[str, Any]:
    """Dashboard summary cards: pair counts, match rate, breaks, notional."""
    pair_count = int(
        session.scalar(
            select(func.count(func.distinct(NormalizedTrade.pair_id))).where(
                NormalizedTrade.pair_id.is_not(None)
            )
        )
        or 0
    )
    broker_leg_count = int(
        session.scalar(
            select(func.count())
            .select_from(NormalizedTrade)
            .where(NormalizedTrade.source == "broker")
        )
        or 0
    )
    desk_leg_count = int(
        session.scalar(
            select(func.count())
            .select_from(NormalizedTrade)
            .where(NormalizedTrade.source == "desk")
        )
        or 0
    )
    matched_pair_count = int(
        session.scalar(
            select(func.count(func.distinct(Match.pair_id))).where(
                Match.pair_id.is_not(None)
            )
        )
        or 0
    )
    match_row_count = int(session.scalar(select(func.count()).select_from(Match)) or 0)
    break_count = int(session.scalar(select(func.count()).select_from(Break)) or 0)
    open_break_count = int(
        session.scalar(
            select(func.count()).select_from(Break).where(Break.status == "open")
        )
        or 0
    )

    type_rows = session.execute(
        select(Break.break_type, func.count())
        .where(Break.status == "open")
        .group_by(Break.break_type)
        .order_by(Break.break_type)
    ).all()
    breaks_by_type = [{"break_type": str(bt), "count": int(n)} for bt, n in type_rows]

    notional = 0.0
    open_breaks = session.scalars(select(Break).where(Break.status == "open")).all()
    for row in open_breaks:
        notional += _break_notional(row)

    return pair_based_summary(
        pair_count=pair_count,
        broker_leg_count=broker_leg_count,
        desk_leg_count=desk_leg_count,
        matched_pair_count=matched_pair_count,
        match_row_count=match_row_count,
        break_count=break_count,
        open_break_count=open_break_count,
        breaks_by_type=breaks_by_type,
        notional_at_risk=notional,
    )


def _break_sort_expression(sort: str):
    if sort == "break_type":
        return Break.break_type
    if sort == "status":
        return Break.status
    if sort == "desk":
        return Break.detail["desk"].astext
    if sort == "symbol":
        return Break.symbol
    if sort == "trade_date":
        return Break.trade_date
    if sort == "notional":
        return func.nullif(Break.detail["notional_at_risk"].astext, "").cast(Float)
    raise ValueError(f"Unsupported break sort field: {sort}")


def _apply_break_filters(
    stmt: Select[tuple[Break]],
    *,
    desk: str | None,
    symbol: str | None,
    break_type: str | None,
    trade_date: date | None,
    date_from: date | None,
    date_to: date | None,
    status: str | None,
) -> Select[tuple[Break]]:
    if symbol:
        stmt = stmt.where(Break.symbol == symbol.upper())
    if break_type:
        stmt = stmt.where(Break.break_type == break_type)
    if trade_date:
        stmt = stmt.where(Break.trade_date == trade_date)
    if date_from:
        stmt = stmt.where(Break.trade_date >= date_from)
    if date_to:
        stmt = stmt.where(Break.trade_date <= date_to)
    if status:
        stmt = stmt.where(Break.status == status)
    if desk:
        stmt = stmt.where(Break.detail["desk"].astext == desk)
    return stmt


def list_breaks(
    session: Session,
    *,
    desk: str | None = None,
    symbol: str | None = None,
    break_type: str | None = None,
    trade_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    sort: BreakSortField | str = "trade_date",
    order: SortOrder | str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Break], int]:
    """Filterable, paginated breaks. Desk filter uses ``detail.desk`` JSON."""
    stmt = _apply_break_filters(
        select(Break),
        desk=desk,
        symbol=symbol,
        break_type=break_type,
        trade_date=trade_date,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = int(session.scalar(count_stmt) or 0)
    offset = (page - 1) * page_size
    sort_key = sort if sort in BREAK_SORT_FIELDS else "trade_date"
    descending = (order or "desc").lower() != "asc"
    column = _break_sort_expression(sort_key)
    ordered = column.desc() if descending else column.asc()
    if hasattr(ordered, "nulls_last"):
        ordered = ordered.nulls_last()
    items = list(
        session.scalars(
            stmt.order_by(ordered, Break.break_id).offset(offset).limit(page_size)
        ).all()
    )
    return items, total


def get_break(session: Session, break_id: UUID) -> Break | None:
    return session.get(Break, break_id)


def latest_suggestion(
    session: Session, break_id: UUID
) -> ResolutionSuggestion | None:
    """Newest suggestion for a break, or None. Uses the ORM relationship."""
    row = session.get(Break, break_id)
    if row is None or not row.suggestions:
        return None
    return max(row.suggestions, key=lambda s: s.created_at)


def list_matches(
    session: Session,
    *,
    symbol: str | None = None,
    trade_date: date | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Match], int]:
    stmt = select(Match)
    if symbol or trade_date:
        broker = select(NormalizedTrade.trade_id).where(
            NormalizedTrade.source == "broker"
        )
        if symbol:
            broker = broker.where(NormalizedTrade.symbol == symbol.upper())
        if trade_date:
            broker = broker.where(NormalizedTrade.trade_date == trade_date)
        stmt = stmt.where(Match.broker_trade_id.in_(broker))
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = int(session.scalar(count_stmt) or 0)
    offset = (page - 1) * page_size
    items = list(
        session.scalars(
            stmt.order_by(Match.created_at.desc(), Match.match_id)
            .offset(offset)
            .limit(page_size)
        ).all()
    )
    return items, total


def get_normalized_by_ids(
    session: Session, source: str, trade_ids: list[str]
) -> list[NormalizedTrade]:
    if not trade_ids:
        return []
    return list(
        session.scalars(
            select(NormalizedTrade).where(
                NormalizedTrade.source == source,
                NormalizedTrade.trade_id.in_(trade_ids),
            )
        ).all()
    )


def get_raw_broker(session: Session, trade_ids: list[str]) -> list[RawBrokerTrade]:
    if not trade_ids:
        return []
    return list(
        session.scalars(
            select(RawBrokerTrade).where(RawBrokerTrade.broker_trade_id.in_(trade_ids))
        ).all()
    )


def get_raw_desk(session: Session, trade_ids: list[str]) -> list[RawDeskTrade]:
    if not trade_ids:
        return []
    return list(
        session.scalars(
            select(RawDeskTrade).where(RawDeskTrade.blotter_id.in_(trade_ids))
        ).all()
    )


def add_audit(
    session: Session,
    *,
    break_id: UUID,
    actor: str,
    action: str,
    suggestion: ResolutionSuggestion | None,
    override_note: str | None,
) -> AuditLog:
    """Always write an audit_log row for a human decision."""
    snapshot: dict[str, Any] | None = None
    suggestion_id = None
    if suggestion is not None:
        suggestion_id = suggestion.suggestion_id
        snapshot = {
            "suggestion_id": str(suggestion.suggestion_id),
            "root_cause": suggestion.root_cause,
            "confidence": suggestion.confidence,
            "explanation": suggestion.explanation,
            "suggested_action": suggestion.suggested_action,
            "evidence": suggestion.evidence,
        }
    row = AuditLog(
        break_id=break_id,
        suggestion_id=suggestion_id,
        actor=actor,
        action=action,
        override_note=override_note,
        agent_suggestion_snapshot=snapshot,
    )
    session.add(row)
    session.flush()
    return row


def trade_ids_for_break(row: Break) -> tuple[list[str], list[str]]:
    """Broker / desk trade ids from columns or detail JSON."""
    broker_ids = parse_trade_ids(row.broker_trade_ids)
    desk_ids = parse_trade_ids(row.desk_trade_ids)
    detail = row.detail or {}
    if not broker_ids and isinstance(detail.get("broker_trade_ids"), list):
        broker_ids = [str(x) for x in detail["broker_trade_ids"]]
    if not desk_ids and isinstance(detail.get("desk_trade_ids"), list):
        desk_ids = [str(x) for x in detail["desk_trade_ids"]]
    return broker_ids, desk_ids
