from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import Session, sessionmaker

if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]

from backend.data.generator import executed_at_from_stable_id, settlement_datetime_et
from backend.db.models import Base, Break, NormalizedTrade, RawBrokerTrade, RawDeskTrade
from backend.ops.backfill_trade_timestamps import backfill_trade_timestamps

NYSE = ZoneInfo("America/New_York")


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            NormalizedTrade.__table__,
            Break.__table__,
            RawBrokerTrade.__table__,
            RawDeskTrade.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, future=True)
    return factory()


def test_executed_at_from_stable_id_is_deterministic_nyse_hours() -> None:
    d = date(2024, 6, 3)
    a = executed_at_from_stable_id("PAIR-abc", d)
    b = executed_at_from_stable_id("PAIR-abc", d)
    assert a == b
    ny = a.astimezone(NYSE)
    minutes = ny.hour * 60 + ny.minute
    assert 9 * 60 + 30 <= minutes < 16 * 60
    assert ny.date() == d


def test_settlement_datetime_is_1600_et() -> None:
    ts = settlement_datetime_et(date(2024, 6, 4))
    ny = ts.astimezone(NYSE)
    assert ny.hour == 16
    assert ny.minute == 0
    assert ny.date() == date(2024, 6, 4)


def test_backfill_fills_nulls_without_changing_trade_date() -> None:
    session = _session()
    now = datetime.now(timezone.utc)
    trade = NormalizedTrade(
        trade_id="BRK-1",
        source="broker",
        symbol="AAPL",
        trade_date=date(2024, 6, 3),
        settlement_date=date(2024, 6, 4),
        side="BUY",
        quantity=100.0,
        price=190.0,
        currency="USD",
        account="CLR-001",
        executing_party="XNYS",
        pair_id="PAIR-1",
        executed_at=None,
        settlement_datetime=None,
        created_at=now,
    )
    brk = Break(
        break_type="price_break",
        status="open",
        pair_id="PAIR-1",
        broker_trade_ids="BRK-1",
        desk_trade_ids="",
        symbol="AAPL",
        trade_date=date(2024, 6, 3),
        executed_at=None,
        created_at=now,
    )
    session.add_all([trade, brk])
    session.flush()

    counts = backfill_trade_timestamps(session)
    assert counts["normalized_executed_at"] == 1
    assert counts["normalized_settlement_datetime"] == 1
    assert counts["breaks_executed_at"] == 1
    assert trade.trade_date == date(2024, 6, 3)
    assert trade.executed_at is not None
    assert trade.settlement_datetime is not None
    settle = trade.settlement_datetime.astimezone(NYSE)
    assert settle.hour == 16
    assert brk.executed_at == trade.executed_at

    again = backfill_trade_timestamps(session)
    assert again["normalized_executed_at"] == 0
    assert again["normalized_settlement_datetime"] == 0
    assert again["breaks_executed_at"] == 0
    session.close()
