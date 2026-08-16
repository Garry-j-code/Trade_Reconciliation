"""Parameterized ORM queries — no free-form SQL from clients."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import DateTime, Float, Select, false, func, literal, select
from sqlalchemy.orm import Session, selectinload

from backend.api.models import (
    BREAK_STATUS_OPEN,
    BREAK_STATUS_OVERRIDDEN,
    BREAK_STATUS_REJECTED,
    BREAK_STATUS_RESOLVED,
)
from backend.api.chart_types import (
    OTHERS_CHART_KEY,
    UNCLASSIFIED_DISPLAY_TYPE,
    displayed_break_category,
    parse_break_type_filter,
    rollup_chart_types,
)
from backend.db.models import (
    AuditLog,
    Break,
    Match,
    NormalizedTrade,
    RawBrokerTrade,
    RawDeskTrade,
    ResolutionSuggestion,
)
from backend.pipeline.rules import as_datetime, parse_trade_ids

BREAK_STATUS_FILTERS: frozenset[str] = frozenset(
    {
        BREAK_STATUS_OPEN,
        BREAK_STATUS_RESOLVED,
        BREAK_STATUS_REJECTED,
        BREAK_STATUS_OVERRIDDEN,
    }
)


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


def _break_executed_at(row: Break) -> datetime | None:
    if row.executed_at is not None:
        return row.executed_at
    detail = row.detail or {}
    return as_datetime(detail.get("executed_at"))


def break_to_list_item(row: Break) -> dict[str, Any]:
    """Serialize a break row plus denormalized desk / notional from detail."""
    return {
        "break_id": row.break_id,
        "break_type": row.break_type,
        "display_type": displayed_break_category(row),
        "status": row.status,
        "symbol": row.symbol,
        "trade_date": row.trade_date,
        "executed_at": _break_executed_at(row),
        "pair_id": row.pair_id,
        "desk": _break_desk(row),
        "notional_at_risk": _break_notional(row),
        "created_at": row.created_at,
        "last_action": None,
        "last_actor": None,
        "last_decided_at": None,
        "last_note": None,
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


def parse_break_status_filter(status: str | None) -> str | None:
    """Map list query ``status`` to a DB value. ``all`` / empty → no filter."""
    if status is None:
        return None
    normalized = status.strip().lower()
    if normalized in {"", "all"}:
        return None
    if normalized not in BREAK_STATUS_FILTERS:
        raise ValueError(
            "status must be one of open, resolved, rejected, overridden, all"
        )
    return normalized


def resolve_date_range(
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    trade_date: date | None = None,
) -> tuple[date | None, date | None]:
    """Inclusive ``(from, to)``. Empty means all dates. ``trade_date`` maps to one day.

    Raises ``ValueError`` when from > to so callers can surface a 422 instead of
    returning an empty result with no explanation.
    """
    start = from_date
    end = to_date
    if trade_date is not None and start is None and end is None:
        start = trade_date
        end = trade_date
    if start is not None and end is not None and start > end:
        raise ValueError("from_date must be on or before to_date")
    return start, end


def trade_date_clauses(column: Any, from_date: date | None, to_date: date | None) -> list[Any]:
    """Inclusive trade_date predicates. Omit both dates → no filter (full book)."""
    clauses: list[Any] = []
    if from_date is not None:
        clauses.append(column >= from_date)
    if to_date is not None:
        clauses.append(column <= to_date)
    return clauses


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
        "break_type_options": [],
        "others_break_types": [],
    }


def summary_stats(
    session: Session,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict[str, Any]:
    """Dashboard summary cards: pair counts, match rate, breaks, notional.

    ``from_date`` / ``to_date`` filter on ``trade_date`` (inclusive). Omit both
    for the full current book.
    """
    start, end = resolve_date_range(from_date=from_date, to_date=to_date)
    trade_range = trade_date_clauses(NormalizedTrade.trade_date, start, end)
    break_range = trade_date_clauses(Break.trade_date, start, end)

    pair_count = int(
        session.scalar(
            select(func.count(func.distinct(NormalizedTrade.pair_id))).where(
                NormalizedTrade.pair_id.is_not(None),
                *trade_range,
            )
        )
        or 0
    )
    broker_leg_count = int(
        session.scalar(
            select(func.count())
            .select_from(NormalizedTrade)
            .where(NormalizedTrade.source == "broker", *trade_range)
        )
        or 0
    )
    desk_leg_count = int(
        session.scalar(
            select(func.count())
            .select_from(NormalizedTrade)
            .where(NormalizedTrade.source == "desk", *trade_range)
        )
        or 0
    )
    in_range_pairs = (
        select(NormalizedTrade.pair_id)
        .where(NormalizedTrade.pair_id.is_not(None), *trade_range)
        .distinct()
    )
    if start is None and end is None:
        matched_pair_count = int(
            session.scalar(
                select(func.count(func.distinct(Match.pair_id))).where(
                    Match.pair_id.is_not(None)
                )
            )
            or 0
        )
        match_row_count = int(session.scalar(select(func.count()).select_from(Match)) or 0)
    else:
        matched_pair_count = int(
            session.scalar(
                select(func.count(func.distinct(Match.pair_id))).where(
                    Match.pair_id.is_not(None),
                    Match.pair_id.in_(in_range_pairs),
                )
            )
            or 0
        )
        match_row_count = int(
            session.scalar(
                select(func.count())
                .select_from(Match)
                .where(Match.pair_id.in_(in_range_pairs))
            )
            or 0
        )
    break_stmt = select(func.count()).select_from(Break)
    if break_range:
        break_stmt = break_stmt.where(*break_range)
    break_count = int(session.scalar(break_stmt) or 0)
    open_break_count = int(
        session.scalar(
            select(func.count())
            .select_from(Break)
            .where(Break.status == "open", *break_range)
        )
        or 0
    )

    open_breaks = session.scalars(
        select(Break)
        .where(Break.status == "open", *break_range)
        .options(selectinload(Break.suggestions))
    ).all()
    notional = 0.0
    type_counts: dict[str, int] = {}
    for row in open_breaks:
        notional += _break_notional(row)
        cat = displayed_break_category(row)
        if cat:
            type_counts[cat] = type_counts.get(cat, 0) + 1
    rolled = rollup_chart_types(type_counts)
    stats = pair_based_summary(
        pair_count=pair_count,
        broker_leg_count=broker_leg_count,
        desk_leg_count=desk_leg_count,
        matched_pair_count=matched_pair_count,
        match_row_count=match_row_count,
        break_count=break_count,
        open_break_count=open_break_count,
        breaks_by_type=rolled.chart,
        notional_at_risk=notional,
    )
    stats["break_type_options"] = rolled.options
    stats["others_break_types"] = rolled.others_members
    return stats


def _display_type_expr() -> Any:
    """SQL: latest suggestion root_cause, else unclassified (not matcher type)."""
    latest_root = (
        select(ResolutionSuggestion.root_cause)
        .where(ResolutionSuggestion.break_id == Break.break_id)
        .order_by(
            ResolutionSuggestion.created_at.desc(),
            ResolutionSuggestion.suggestion_id.desc(),
        )
        .limit(1)
        .scalar_subquery()
    )
    return func.coalesce(latest_root, literal(UNCLASSIFIED_DISPLAY_TYPE))


def display_type_counts(
    session: Session,
    *,
    desk: str | None = None,
    symbol: str | None = None,
    trade_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
) -> dict[str, int]:
    """Observed display categories for the current filters (not the type filter)."""
    cat = _display_type_expr()
    stmt = select(cat, func.count()).select_from(Break)
    stmt = _apply_break_filters(
        stmt,
        desk=desk,
        symbol=symbol,
        break_type=None,
        trade_date=trade_date,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    stmt = stmt.group_by(cat)
    return {
        str(key): int(n)
        for key, n in session.execute(stmt).all()
        if key is not None and str(key)
    }


def display_type_catalog(
    session: Session,
    *,
    desk: str | None = None,
    symbol: str | None = None,
    trade_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
) -> tuple[list[str], list[str]]:
    rolled = rollup_chart_types(
        display_type_counts(
            session,
            desk=desk,
            symbol=symbol,
            trade_date=trade_date,
            date_from=date_from,
            date_to=date_to,
            status=status,
        )
    )
    return rolled.options, rolled.others_members


def _resolved_display_types(
    session: Session,
    break_type: str | None,
    *,
    desk: str | None,
    symbol: str | None,
    trade_date: date | None,
    date_from: date | None,
    date_to: date | None,
    status: str | None,
) -> list[str] | None:
    tokens = parse_break_type_filter(break_type)
    if not tokens:
        return None
    if tokens == [OTHERS_CHART_KEY]:
        return rollup_chart_types(
            display_type_counts(
                session,
                desk=desk,
                symbol=symbol,
                trade_date=trade_date,
                date_from=date_from,
                date_to=date_to,
                status=status,
            )
        ).others_members
    return tokens


def _break_sort_expression(sort: str):
    if sort == "break_type":
        return _display_type_expr()
    if sort == "status":
        return Break.status
    if sort == "desk":
        return Break.detail["desk"].astext
    if sort == "symbol":
        return Break.symbol
    if sort == "trade_date":
        return func.coalesce(
            Break.executed_at,
            Break.trade_date.cast(DateTime(timezone=True)),
        )
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
    start, end = resolve_date_range(
        from_date=date_from, to_date=date_to, trade_date=trade_date
    )
    if symbol:
        stmt = stmt.where(Break.symbol == symbol.upper())
    display_types = parse_break_type_filter(break_type)
    if display_types:
        if display_types == [OTHERS_CHART_KEY]:
            stmt = stmt.where(false())
        elif len(display_types) == 1:
            stmt = stmt.where(_display_type_expr() == display_types[0])
        else:
            stmt = stmt.where(_display_type_expr().in_(display_types))
    for clause in trade_date_clauses(Break.trade_date, start, end):
        stmt = stmt.where(clause)
    status_filter = parse_break_status_filter(status)
    if status_filter:
        stmt = stmt.where(Break.status == status_filter)
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
    resolved = _resolved_display_types(
        session,
        break_type,
        desk=desk,
        symbol=symbol,
        trade_date=trade_date,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    type_filter: str | None
    if resolved is None:
        type_filter = None
    elif not resolved:
        type_filter = OTHERS_CHART_KEY
    else:
        type_filter = ",".join(resolved)
    stmt = _apply_break_filters(
        select(Break),
        desk=desk,
        symbol=symbol,
        break_type=type_filter,
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
            stmt.options(selectinload(Break.suggestions))
            .order_by(ordered, Break.break_id)
            .offset(offset)
            .limit(page_size)
        ).all()
    )
    return items, total


def list_audits_for_break(session: Session, break_id: UUID) -> list[AuditLog]:
    """Chronological human decisions for one break."""
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.break_id == break_id)
            .order_by(AuditLog.created_at.asc(), AuditLog.audit_id.asc())
        ).all()
    )


def latest_audits_by_break(
    session: Session, break_ids: list[UUID]
) -> dict[UUID, AuditLog]:
    """Newest audit_log row per break_id."""
    if not break_ids:
        return {}
    rows = list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.break_id.in_(break_ids))
            .order_by(AuditLog.created_at.desc(), AuditLog.audit_id.desc())
        ).all()
    )
    latest: dict[UUID, AuditLog] = {}
    for row in rows:
        if row.break_id is not None and row.break_id not in latest:
            latest[row.break_id] = row
    return latest


def apply_latest_audit(item: dict[str, Any], audit: AuditLog | None) -> dict[str, Any]:
    if audit is None:
        return item
    item["last_action"] = audit.action
    item["last_actor"] = audit.actor
    item["last_decided_at"] = audit.created_at
    item["last_note"] = audit.override_note
    return item


def audit_to_decision(row: AuditLog) -> dict[str, Any]:
    snap = row.agent_suggestion_snapshot if isinstance(row.agent_suggestion_snapshot, dict) else {}
    return {
        "audit_id": row.audit_id,
        "actor": row.actor,
        "action": row.action,
        "override_note": row.override_note,
        "created_at": row.created_at,
        "suggestion_id": row.suggestion_id,
        "root_cause": snap.get("root_cause"),
        "suggested_action": snap.get("suggested_action"),
        "explanation": snap.get("explanation"),
    }


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
