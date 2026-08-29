"""Fill missing executed_at / settlement_datetime on existing RDS rows.

Does not change trade_date or settlement_date. Execution times use the same
NYSE-hours helper as the generator, seeded from pair_id or trade_id.

Never prints DATABASE_URL.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from backend.data.generator import executed_at_from_stable_id, settlement_datetime_et
from backend.db.models import Break, NormalizedTrade, RawBrokerTrade, RawDeskTrade
from backend.db.session import (
    database_url_from_env,
    ensure_agent_schema_patches,
    get_engine,
    get_session_factory,
    session_scope,
)
from backend.pipeline.rules import parse_trade_ids

logger = logging.getLogger(__name__)


def _stable_key(*parts: str | None) -> str:
    for part in parts:
        text = (part or "").strip()
        if text:
            return text
    return "unknown"


def _stamp_executed(trade_date: date | None, *ids: str | None) -> datetime | None:
    if trade_date is None:
        return None
    return executed_at_from_stable_id(_stable_key(*ids), trade_date)


def _stamp_settle(settle: date | None) -> datetime | None:
    if settle is None:
        return None
    return settlement_datetime_et(settle)


def backfill_trade_timestamps(session: Session) -> dict[str, int]:
    """Idempotent: only rows with null timestamps are updated."""
    counts = {
        "normalized_executed_at": 0,
        "normalized_settlement_datetime": 0,
        "raw_broker_executed_at": 0,
        "raw_broker_settlement_datetime": 0,
        "raw_desk_executed_at": 0,
        "raw_desk_settlement_datetime": 0,
        "breaks_executed_at": 0,
    }

    by_trade_id: dict[str, datetime] = {}

    for row in session.query(NormalizedTrade).all():
        if row.executed_at is None and row.trade_date is not None:
            row.executed_at = _stamp_executed(
                row.trade_date, row.pair_id, row.trade_id
            )
            counts["normalized_executed_at"] += 1
        if row.settlement_datetime is None and row.settlement_date is not None:
            row.settlement_datetime = _stamp_settle(row.settlement_date)
            counts["normalized_settlement_datetime"] += 1
        if row.executed_at is not None:
            by_trade_id[row.trade_id] = row.executed_at

    for row in session.query(RawBrokerTrade).all():
        if row.executed_at is None and row.trade_date is not None:
            row.executed_at = _stamp_executed(
                row.trade_date, row.pair_id, row.broker_trade_id
            )
            counts["raw_broker_executed_at"] += 1
        if row.settlement_datetime is None and row.settlement_date is not None:
            row.settlement_datetime = _stamp_settle(row.settlement_date)
            counts["raw_broker_settlement_datetime"] += 1

    for row in session.query(RawDeskTrade).all():
        if row.executed_at is None and row.trade_date is not None:
            row.executed_at = _stamp_executed(
                row.trade_date, row.pair_id, row.blotter_id
            )
            counts["raw_desk_executed_at"] += 1
        if row.settlement_datetime is None and row.settle_date is not None:
            row.settlement_datetime = _stamp_settle(row.settle_date)
            counts["raw_desk_settlement_datetime"] += 1

    for row in session.query(Break).all():
        if row.executed_at is not None:
            continue
        times: list[datetime] = []
        for tid in parse_trade_ids(row.broker_trade_ids) + parse_trade_ids(
            row.desk_trade_ids
        ):
            ts = by_trade_id.get(tid)
            if ts is not None:
                times.append(ts)
        if times:
            row.executed_at = min(times)
            counts["breaks_executed_at"] += 1
        elif row.trade_date is not None:
            row.executed_at = _stamp_executed(
                row.trade_date, row.pair_id, str(row.break_id)
            )
            counts["breaks_executed_at"] += 1

    session.flush()
    return counts


def main(argv: list[str] | None = None) -> int:
    del argv  # CLI has no flags; DATABASE_URL comes from the environment.
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    url = database_url_from_env()
    if not url:
        logger.error("DATABASE_URL is not set")
        return 1
    engine = get_engine(url)
    ensure_agent_schema_patches(engine)
    factory = get_session_factory(engine)
    with session_scope(factory) as session:
        counts = backfill_trade_timestamps(session)
    logger.info("Backfill complete: %s", counts)
    print("Backfill complete")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
