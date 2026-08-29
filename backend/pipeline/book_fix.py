"""Human-approved book mutations. Deterministic — no LLM, no new prices.

The agent only writes ``resolution_suggestions``. After a human Approve POST,
this module copies fields already on the two legs (or voids an extra duplicate)
and rematches so downstream matches/breaks stay consistent.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db.models import Break, Match, NormalizedTrade, ResolutionSuggestion
from backend.pipeline.matcher import (
    load_frames_to_db,
    match_trades,
    normalized_orm_to_record,
)
from backend.pipeline.normalize import CANONICAL_COLUMNS, SOURCE_BROKER, SOURCE_DESK
from backend.pipeline.rules import (
    BREAK_DUPLICATE,
    BREAK_MISSING_BROKER,
    BREAK_MISSING_DESK,
    BREAK_PRICE,
    BREAK_QUANTITY,
    BREAK_SETTLEMENT,
    parse_trade_ids,
)

Authority = Literal["broker", "desk"]

BOOK_FIX_BREAK_TYPES: frozenset[str] = frozenset(
    {
        BREAK_PRICE,
        BREAK_QUANTITY,
        BREAK_DUPLICATE,
        BREAK_MISSING_BROKER,
        BREAK_MISSING_DESK,
        BREAK_SETTLEMENT,
    }
)

WORKFLOW_ONLY_ACTIONS: frozenset[str] = frozenset(
    {
        "wait_for_corporate_action",
        "escalate_to_ops",
        "no_action",
    }
)

NO_SUGGESTION_MESSAGE = (
    "Investigate this break before approving so a suggested_action can be "
    "applied to the books. Price, quantity, duplicate, missing-trade, and "
    "settlement breaks cannot be resolved while the two sides still disagree. "
    "Run Investigate, or use Override with a note to close without a book fix."
)


class BookFixError(ValueError):
    """Raised when Approve cannot apply the suggested book mutation."""


def needs_suggestion_to_approve(break_type: str) -> bool:
    """True when silently resolving would leave an economic mismatch on the book."""
    return str(break_type) in BOOK_FIX_BREAK_TYPES


def authority_side(action: str, root_cause: str) -> Authority:
    """Which existing leg is source of truth. Never invents a third price/qty."""
    if action == "accept_broker":
        return "broker"
    if action == "accept_desk":
        return "desk"
    if root_cause == "desk_booking_error":
        return "broker"
    if root_cause == "broker_reporting_lag":
        return "desk"
    return "broker"


def apply_suggested_action(
    session: Session,
    brk: Break,
    suggestion: ResolutionSuggestion,
) -> dict[str, str | None]:
    """Mutate ``normalized_trades`` per ``suggested_action``, then rematch.

    Workflow-only actions (wait / escalate / no_action) do not change prices.
    """
    action = str(suggestion.suggested_action)
    root = str(suggestion.root_cause)
    if action in WORKFLOW_ONLY_ACTIONS:
        return {"action": action, "authority": None, "mutated": "false"}

    authority = authority_side(action, root)
    broker_ids, desk_ids = _trade_ids_for_break(brk)
    broker = _normalized_by_ids(session, SOURCE_BROKER, broker_ids)
    desk = _normalized_by_ids(session, SOURCE_DESK, desk_ids)

    if action == "amend_price":
        _copy_field(broker, desk, authority, "price")
    elif action == "amend_quantity":
        _copy_field(broker, desk, authority, "quantity")
    elif action == "amend_settlement_date":
        _copy_field(broker, desk, authority, "settlement_date")
        _copy_field(broker, desk, authority, "settlement_datetime")
    elif action == "accept_broker":
        _copy_economic_fields(src=broker, dest=desk, label="desk")
    elif action == "accept_desk":
        _copy_economic_fields(src=desk, dest=broker, label="broker")
    elif action == "cancel_duplicate":
        _cancel_duplicate(session, broker, desk, broker_ids, desk_ids)
    elif action == "book_missing_trade":
        _book_missing(session, brk, broker, desk)
    else:
        raise BookFixError(f"Unsupported suggested_action {action!r}")

    session.flush()
    rematch_book(session, keep_break_ids={brk.break_id})
    return {"action": action, "authority": authority, "mutated": "true"}


def rematch_book(session: Session, *, keep_break_ids: set[UUID] | None = None) -> None:
    """Rematch the in-session normalized book. Does not call an LLM."""
    rows = list(session.scalars(select(NormalizedTrade)).all())
    if not rows:
        frame = pd.DataFrame(columns=list(CANONICAL_COLUMNS))
    else:
        frame = pd.DataFrame([normalized_orm_to_record(r) for r in rows])
    result = match_trades(frame)
    load_frames_to_db(
        result.matches,
        result.breaks,
        session,
        replace=True,
        keep_break_ids=keep_break_ids,
    )


def _require_both_sides(
    broker: list[NormalizedTrade],
    desk: list[NormalizedTrade],
    action: str,
) -> None:
    if not broker or not desk:
        raise BookFixError(
            f"Cannot apply {action}: need both broker and desk normalized legs."
        )


def _copy_field(
    broker: list[NormalizedTrade],
    desk: list[NormalizedTrade],
    authority: Authority,
    field: str,
) -> None:
    _require_both_sides(broker, desk, f"amend {field}")
    src = broker if authority == "broker" else desk
    dest = desk if authority == "broker" else broker
    value = getattr(src[0], field)
    for row in dest:
        setattr(row, field, value)


def _copy_economic_fields(
    *,
    src: list[NormalizedTrade],
    dest: list[NormalizedTrade],
    label: str,
) -> None:
    if not src or not dest:
        raise BookFixError(
            f"Cannot accept that side: missing {label} or opposing normalized legs."
        )
    template = src[0]
    for row in dest:
        row.price = template.price
        row.quantity = template.quantity
        row.settlement_date = template.settlement_date
        row.settlement_datetime = template.settlement_datetime


def _cancel_duplicate(
    session: Session,
    broker: list[NormalizedTrade],
    desk: list[NormalizedTrade],
    broker_ids: list[str],
    desk_ids: list[str],
) -> None:
    if len(broker) >= 2:
        keep = broker_ids[0] if broker_ids else sorted(t.trade_id for t in broker)[0]
        extra = [t for t in broker if t.trade_id != keep]
        if not extra:
            extra = broker[1:]
        for row in extra:
            session.delete(row)
        return
    if len(desk) >= 2:
        keep = desk_ids[0] if desk_ids else sorted(t.trade_id for t in desk)[0]
        extra = [t for t in desk if t.trade_id != keep]
        if not extra:
            extra = desk[1:]
        for row in extra:
            session.delete(row)
        return
    raise BookFixError(
        "Cannot apply cancel_duplicate: no extra broker or desk row to void."
    )


def _book_missing(
    session: Session,
    brk: Break,
    broker: list[NormalizedTrade],
    desk: list[NormalizedTrade],
) -> None:
    if brk.break_type == BREAK_MISSING_DESK or (broker and not desk):
        if not broker:
            raise BookFixError("Cannot book missing desk trade: no broker leg to copy.")
        for src in broker:
            session.add(_clone_leg(src, new_source=SOURCE_DESK))
        return
    if brk.break_type == BREAK_MISSING_BROKER or (desk and not broker):
        if not desk:
            raise BookFixError("Cannot book missing broker trade: no desk leg to copy.")
        for src in desk:
            session.add(_clone_leg(src, new_source=SOURCE_BROKER))
        return
    raise BookFixError(
        "Cannot apply book_missing_trade: both sides already have normalized rows."
    )


def _clone_leg(src: NormalizedTrade, *, new_source: str) -> NormalizedTrade:
    suffix = "desk" if new_source == SOURCE_DESK else "broker"
    return NormalizedTrade(
        trade_id=f"{src.trade_id}__booked_{suffix}",
        source=new_source,
        symbol=src.symbol,
        trade_date=src.trade_date,
        executed_at=src.executed_at,
        settlement_date=src.settlement_date,
        settlement_datetime=src.settlement_datetime,
        side=src.side,
        quantity=src.quantity,
        price=src.price,
        currency=src.currency,
        account=src.account,
        executing_party=src.executing_party,
        pair_id=src.pair_id,
        raw_payload={
            "booked_from_trade_id": src.trade_id,
            "booked_from_source": src.source,
        },
    )


def _trade_ids_for_break(row: Break) -> tuple[list[str], list[str]]:
    broker_ids = parse_trade_ids(row.broker_trade_ids)
    desk_ids = parse_trade_ids(row.desk_trade_ids)
    detail = row.detail or {}
    if not broker_ids and isinstance(detail.get("broker_trade_ids"), list):
        broker_ids = [str(x) for x in detail["broker_trade_ids"]]
    if not desk_ids and isinstance(detail.get("desk_trade_ids"), list):
        desk_ids = [str(x) for x in detail["desk_trade_ids"]]
    return broker_ids, desk_ids


def _normalized_by_ids(
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


def match_exists(session: Session, broker_trade_id: str, desk_trade_id: str) -> bool:
    row = session.scalars(
        select(Match).where(
            Match.broker_trade_id == broker_trade_id,
            Match.desk_trade_id == desk_trade_id,
        )
    ).first()
    return row is not None
