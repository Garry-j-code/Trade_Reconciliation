"""Approve applies book mutations. No LLM."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import Session, sessionmaker

if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]

from backend.api.models import AUDIT_APPROVED, AUDIT_REJECTED, BREAK_STATUS_REJECTED
from backend.api.services import approve_break, reject_break
from backend.db.models import (
    AuditLog,
    Base,
    Break,
    Match,
    NormalizedTrade,
    ResolutionSuggestion,
)
from backend.pipeline.rules import BREAK_DUPLICATE, BREAK_PRICE


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    tables = [
        NormalizedTrade.__table__,
        Match.__table__,
        Break.__table__,
        ResolutionSuggestion.__table__,
        AuditLog.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine, autoflush=False, future=True)
    return factory()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _trade(
    *,
    source: str,
    trade_id: str,
    price: float,
    quantity: float = 2200.0,
    pair_id: str = "PAIR-PX",
) -> NormalizedTrade:
    return NormalizedTrade(
        trade_id=trade_id,
        source=source,
        symbol="AAPL",
        trade_date=date(2024, 6, 3),
        settlement_date=date(2024, 6, 4),
        side="BUY",
        quantity=quantity,
        price=price,
        currency="USD",
        account="EQ-US" if source == "desk" else "CLR-1",
        executing_party="J.KIM" if source == "desk" else "XNYS",
        pair_id=pair_id,
        created_at=_now(),
    )


def _suggest(brk: Break, *, action: str, root: str) -> ResolutionSuggestion:
    return ResolutionSuggestion(
        suggestion_id=uuid4(),
        break_id=brk.break_id,
        root_cause=root,
        confidence=0.8,
        explanation="copy the clearing print",
        suggested_action=action,
        evidence=[],
        created_at=_now(),
    )


def test_approve_applies_amend_price() -> None:
    session = _session()
    broker = _trade(source="broker", trade_id="BRK-1", price=190.0)
    desk = _trade(source="desk", trade_id="DSK-1", price=191.5)
    brk = Break(
        break_id=uuid4(),
        break_type=BREAK_PRICE,
        status="open",
        pair_id="PAIR-PX",
        broker_trade_ids="BRK-1",
        desk_trade_ids="DSK-1",
        symbol="AAPL",
        trade_date=date(2024, 6, 3),
        detail={"notional_at_risk": 1000.0},
        created_at=_now(),
    )
    sugg = _suggest(brk, action="amend_price", root="price_mismatch")
    brk.suggestions = [sugg]
    session.add_all([broker, desk, brk, sugg])
    session.flush()

    result = approve_break(session, brk.break_id, actor="analyst@traderecon.demo")
    assert result.status == "resolved"
    assert result.action == AUDIT_APPROVED

    desk_row = session.scalars(
        select(NormalizedTrade).where(
            NormalizedTrade.source == "desk", NormalizedTrade.trade_id == "DSK-1"
        )
    ).one()
    broker_row = session.scalars(
        select(NormalizedTrade).where(
            NormalizedTrade.source == "broker", NormalizedTrade.trade_id == "BRK-1"
        )
    ).one()
    assert desk_row.price == pytest.approx(190.0)
    assert broker_row.price == pytest.approx(190.0)
    match = session.scalars(select(Match)).first()
    assert match is not None
    assert match.broker_trade_id == "BRK-1"
    assert match.desk_trade_id == "DSK-1"
    kept = session.get(Break, brk.break_id)
    assert kept is not None
    assert kept.status == "resolved"
    audit = session.scalars(select(AuditLog)).one()
    assert audit.actor == "analyst@traderecon.demo"
    session.close()


def test_approve_applies_cancel_duplicate() -> None:
    session = _session()
    b1 = _trade(source="broker", trade_id="BRK-1", price=50.0, quantity=2200.0)
    b2 = _trade(source="broker", trade_id="BRK-2", price=50.0, quantity=2200.0)
    desk = _trade(source="desk", trade_id="DSK-1", price=50.0, quantity=2200.0)
    brk = Break(
        break_id=uuid4(),
        break_type=BREAK_DUPLICATE,
        status="open",
        pair_id="PAIR-PX",
        broker_trade_ids="BRK-1,BRK-2",
        desk_trade_ids="DSK-1",
        symbol="AAPL",
        trade_date=date(2024, 6, 3),
        created_at=_now(),
    )
    sugg = _suggest(brk, action="cancel_duplicate", root="duplicate_booking")
    brk.suggestions = [sugg]
    session.add_all([b1, b2, desk, brk, sugg])
    session.flush()

    approve_break(session, brk.break_id, actor="ops")
    broker_ids = {
        row.trade_id
        for row in session.scalars(
            select(NormalizedTrade).where(NormalizedTrade.source == "broker")
        ).all()
    }
    assert broker_ids == {"BRK-1"}
    desk_ids = {
        row.trade_id
        for row in session.scalars(
            select(NormalizedTrade).where(NormalizedTrade.source == "desk")
        ).all()
    }
    assert desk_ids == {"DSK-1"}
    assert session.scalars(select(Match)).first() is not None
    session.close()


def test_reject_does_not_mutate() -> None:
    session = _session()
    broker = _trade(source="broker", trade_id="BRK-1", price=190.0)
    desk = _trade(source="desk", trade_id="DSK-1", price=191.5)
    brk = Break(
        break_id=uuid4(),
        break_type=BREAK_PRICE,
        status="open",
        pair_id="PAIR-PX",
        broker_trade_ids="BRK-1",
        desk_trade_ids="DSK-1",
        symbol="AAPL",
        trade_date=date(2024, 6, 3),
        created_at=_now(),
    )
    sugg = _suggest(brk, action="amend_price", root="price_mismatch")
    brk.suggestions = [sugg]
    session.add_all([broker, desk, brk, sugg])
    session.flush()

    rejected = reject_break(session, brk.break_id, actor="analyst", note="not the print")
    assert rejected.status == BREAK_STATUS_REJECTED
    desk_row = session.scalars(
        select(NormalizedTrade).where(NormalizedTrade.trade_id == "DSK-1")
    ).one()
    assert desk_row.price == pytest.approx(191.5)
    assert session.scalars(select(Match)).first() is None
    audit = session.scalars(select(AuditLog)).one()
    assert audit.action == AUDIT_REJECTED
    assert audit.override_note == "not the print"
    session.close()
