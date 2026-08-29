"""Ingest generated Parquet → normalize → write local Parquet and/or Postgres.

No LLM, no live market-API calls. DB load is optional (requires DATABASE_URL).

Usage:
    uv run normalize-trades
    uv run python -m backend.pipeline.ingest

Env:
    TRADE_OUTPUT_DIR     (default: backend/data/generated)
    NORMALIZED_OUTPUT_DIR (default: backend/data/normalized)
    DATABASE_URL         (optional; if set, also load into Postgres)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.db.models import NormalizedTrade, RawBrokerTrade, RawDeskTrade
from backend.db.session import (
    create_all_tables,
    database_url_from_env,
    get_engine,
    get_session_factory,
    session_scope,
)
from backend.pipeline.normalize import (
    CANONICAL_COLUMNS,
    NormalizationError,
    normalize_both,
    prepare_normalized_for_parquet,
)
from backend.pipeline.rules import as_datetime

logger = logging.getLogger(__name__)

BROKER_FILENAME = "broker_trades.parquet"
DESK_FILENAME = "desk_trades.parquet"
NORMALIZED_FILENAME = "normalized_trades.parquet"


@dataclass(frozen=True)
class IngestPaths:
    input_dir: Path
    output_dir: Path
    broker: Path
    desk: Path
    normalized: Path


def default_trade_input_dir() -> Path:
    override = os.environ.get("TRADE_OUTPUT_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "data" / "generated"


def default_normalized_output_dir() -> Path:
    override = os.environ.get("NORMALIZED_OUTPUT_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "data" / "normalized"


def resolve_paths(
    input_dir: Path | None = None,
    output_dir: Path | None = None,
) -> IngestPaths:
    inp = Path(input_dir) if input_dir is not None else default_trade_input_dir()
    out = Path(output_dir) if output_dir is not None else default_normalized_output_dir()
    return IngestPaths(
        input_dir=inp,
        output_dir=out,
        broker=inp / BROKER_FILENAME,
        desk=inp / DESK_FILENAME,
        normalized=out / NORMALIZED_FILENAME,
    )


def read_generated_trades(
    input_dir: Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read broker + desk Parquet produced by ``generate-trades``."""
    paths = resolve_paths(input_dir=input_dir)
    if not paths.broker.exists():
        raise FileNotFoundError(f"Missing broker trades: {paths.broker}")
    if not paths.desk.exists():
        raise FileNotFoundError(f"Missing desk trades: {paths.desk}")
    broker = pd.read_parquet(paths.broker)
    desk = pd.read_parquet(paths.desk)
    return broker, desk


def write_normalized_parquet(df: pd.DataFrame, path: Path) -> Path:
    """Write canonical trades to Parquet (JSON-string raw_payload)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    prepared = prepare_normalized_for_parquet(df)
    prepared.to_parquet(path, index=False)
    return path


def _as_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if value is None or (isinstance(value, float) and pd.isna(value)):
        raise ValueError("date value is null")
    return pd.to_datetime(value).date()


def frame_trade_dates(df: pd.DataFrame, column: str = "trade_date") -> pd.Series:
    """Coerce a date column to ``datetime.date`` values."""
    if df.empty or column not in df.columns:
        return pd.Series(dtype=object)
    return pd.to_datetime(df[column]).dt.date


def filter_frame_to_trade_date(
    df: pd.DataFrame, trade_date: date, column: str = "trade_date"
) -> pd.DataFrame:
    """Keep rows whose ``column`` equals ``trade_date``."""
    if df.empty:
        return df
    dates = frame_trade_dates(df, column)
    return df.loc[dates == trade_date].copy().reset_index(drop=True)


def merge_parquet_by_trade_date(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    trade_date: date,
    column: str = "trade_date",
) -> pd.DataFrame:
    """Drop ``trade_date`` from ``existing`` and append ``incoming``."""
    if existing is None or existing.empty:
        return incoming.reset_index(drop=True)
    dates = frame_trade_dates(existing, column)
    keep = existing.loc[dates != trade_date]
    return pd.concat([keep, incoming], ignore_index=True)


def _as_float(value: Any) -> float:
    return float(value)


def raw_broker_row_to_orm(row: Mapping[str, Any]) -> RawBrokerTrade:
    return RawBrokerTrade(
        broker_trade_id=str(row["broker_trade_id"]),
        symbol=str(row["symbol"]),
        trade_date=_as_date(row["trade_date"]),
        settlement_date=_as_date(row["settlement_date"]),
        settlement_datetime=as_datetime(row.get("settlement_datetime")),
        side=str(row["side"]),
        quantity=_as_float(row["quantity"]),
        price=_as_float(row["price"]),
        currency=str(row["currency"]),
        account_id=str(row["account_id"]),
        execution_venue=str(row["execution_venue"]),
        pair_id=str(row["pair_id"]) if pd.notna(row.get("pair_id")) else None,
        executed_at=as_datetime(row.get("executed_at")),
    )


def raw_desk_row_to_orm(row: Mapping[str, Any]) -> RawDeskTrade:
    return RawDeskTrade(
        blotter_id=str(row["blotter_id"]),
        ticker=str(row["ticker"]),
        trade_date=_as_date(row["trade_date"]),
        settle_date=_as_date(row["settle_date"]),
        settlement_datetime=as_datetime(row.get("settlement_datetime")),
        side=str(row["side"]),
        qty=_as_float(row["qty"]),
        px=_as_float(row["px"]),
        ccy=str(row["ccy"]),
        desk_code=str(row["desk_code"]),
        trader=str(row["trader"]),
        pair_id=str(row["pair_id"]) if pd.notna(row.get("pair_id")) else None,
        executed_at=as_datetime(row.get("executed_at")),
    )


def normalized_row_to_orm(row: Mapping[str, Any]) -> NormalizedTrade:
    payload = row.get("raw_payload")
    if isinstance(payload, str):
        payload = json.loads(payload)
    elif payload is not None and not isinstance(payload, dict):
        payload = None
    return NormalizedTrade(
        trade_id=str(row["trade_id"]),
        source=str(row["source"]),
        symbol=str(row["symbol"]),
        trade_date=_as_date(row["trade_date"]),
        settlement_date=_as_date(row["settlement_date"]),
        settlement_datetime=as_datetime(row.get("settlement_datetime")),
        side=str(row["side"]),
        quantity=_as_float(row["quantity"]),
        price=_as_float(row["price"]),
        currency=str(row["currency"]),
        account=str(row["account"]),
        executing_party=str(row["executing_party"]),
        pair_id=str(row["pair_id"]) if pd.notna(row.get("pair_id")) else None,
        executed_at=as_datetime(row.get("executed_at")),
        raw_payload=payload,
    )


def load_frames_to_db(
    broker_raw: pd.DataFrame,
    desk_raw: pd.DataFrame,
    normalized: pd.DataFrame,
    session: Session,
    *,
    replace: bool = True,
    trade_date: date | None = None,
) -> dict[str, int]:
    """Insert raw + normalized rows.

    When ``trade_date`` is set, delete/replace **only that session**.
    When ``replace`` and no date, truncate those tables first (legacy wipe).
    """
    broker_in = broker_raw
    desk_in = desk_raw
    norm_in = normalized
    if trade_date is not None:
        broker_in = filter_frame_to_trade_date(broker_raw, trade_date)
        desk_in = filter_frame_to_trade_date(desk_raw, trade_date)
        norm_in = filter_frame_to_trade_date(normalized, trade_date)
        session.execute(
            delete(NormalizedTrade).where(NormalizedTrade.trade_date == trade_date)
        )
        session.execute(
            delete(RawBrokerTrade).where(RawBrokerTrade.trade_date == trade_date)
        )
        session.execute(
            delete(RawDeskTrade).where(RawDeskTrade.trade_date == trade_date)
        )
    elif replace:
        session.execute(delete(NormalizedTrade))
        session.execute(delete(RawBrokerTrade))
        session.execute(delete(RawDeskTrade))

    broker_orms = [
        raw_broker_row_to_orm(r) for r in broker_in.to_dict(orient="records")
    ]
    desk_orms = [raw_desk_row_to_orm(r) for r in desk_in.to_dict(orient="records")]
    norm_orms = [
        normalized_row_to_orm(r) for r in norm_in.to_dict(orient="records")
    ]
    session.add_all(broker_orms)
    session.add_all(desk_orms)
    session.add_all(norm_orms)
    session.flush()
    return {
        "raw_broker": len(broker_orms),
        "raw_desk": len(desk_orms),
        "normalized": len(norm_orms),
        "trade_date": trade_date.isoformat() if trade_date else None,
    }


def load_to_database(
    broker_raw: pd.DataFrame,
    desk_raw: pd.DataFrame,
    normalized: pd.DataFrame,
    database_url: str,
    *,
    replace: bool = True,
    trade_date: date | None = None,
) -> dict[str, int]:
    """Create schema if needed and load frames into Postgres."""
    engine = get_engine(database_url)
    create_all_tables(engine)
    factory = get_session_factory(engine)
    with session_scope(factory) as session:
        counts = load_frames_to_db(
            broker_raw,
            desk_raw,
            normalized,
            session,
            replace=replace,
            trade_date=trade_date,
        )
    return counts


@dataclass
class NormalizeRunResult:
    broker_rows: int
    desk_rows: int
    normalized_rows: int
    parquet_path: Path
    db_loaded: bool
    db_counts: dict[str, int] | None


def run_normalize(
    *,
    input_dir: Path | None = None,
    output_dir: Path | None = None,
    database_url: str | None = None,
    write_parquet: bool = True,
    load_db: bool | None = None,
    include_raw_payload: bool = True,
    trade_date: date | None = None,
    replace: bool = True,
) -> NormalizeRunResult:
    """Read generated trades → normalize → write Parquet; optionally load DB.

    ``load_db`` defaults to True when ``database_url`` (or env) is set.
    When ``trade_date`` is set, Parquet and DB replace **that session only**.
    """
    paths = resolve_paths(input_dir=input_dir, output_dir=output_dir)
    broker, desk = read_generated_trades(paths.input_dir)
    if trade_date is not None:
        broker = filter_frame_to_trade_date(broker, trade_date)
        desk = filter_frame_to_trade_date(desk, trade_date)
    normalized = normalize_both(
        broker, desk, include_raw_payload=include_raw_payload
    )

    parquet_path = paths.normalized
    if write_parquet:
        if trade_date is not None and parquet_path.exists():
            existing = pd.read_parquet(parquet_path)
            merged = merge_parquet_by_trade_date(existing, normalized, trade_date)
            write_normalized_parquet(merged, parquet_path)
            logger.info(
                "Wrote %s (%d rows; replaced %s)",
                parquet_path,
                len(merged),
                trade_date.isoformat(),
            )
        else:
            write_normalized_parquet(normalized, parquet_path)
            logger.info("Wrote %s (%d rows)", parquet_path, len(normalized))

    url = database_url if database_url is not None else database_url_from_env()
    should_load = load_db if load_db is not None else bool(url)
    db_counts: dict[str, int] | None = None
    if should_load:
        if not url:
            raise ValueError("load_db requested but DATABASE_URL is not set")
        db_counts = load_to_database(
            broker, desk, normalized, url, replace=replace, trade_date=trade_date
        )
        logger.info("Loaded into DB: %s", db_counts)

    return NormalizeRunResult(
        broker_rows=len(broker),
        desk_rows=len(desk),
        normalized_rows=len(normalized),
        parquet_path=parquet_path,
        db_loaded=bool(db_counts),
        db_counts=db_counts,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize synthetic broker + desk trades into canonical schema; "
            "write Parquet and optionally load Postgres when DATABASE_URL is set."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=None,
        help="Directory with broker_trades.parquet / desk_trades.parquet",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to write normalized_trades.parquet",
    )
    parser.add_argument(
        "--no-parquet",
        action="store_true",
        help="Skip writing local Parquet (DB-only when DATABASE_URL is set)",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Never load Postgres even if DATABASE_URL is set",
    )
    parser.add_argument(
        "--load-db",
        action="store_true",
        help="Require DB load (error if DATABASE_URL missing)",
    )
    parser.add_argument(
        "--trade-date",
        type=str,
        default=None,
        help="Replace only this session (YYYY-MM-DD) in Parquet/DB; other dates stay",
    )
    parser.add_argument(
        "--replace-all",
        action="store_true",
        help="Truncate raw/normalized tables (legacy wipe). Ignored when --trade-date is set",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    load_db: bool | None
    if args.no_db:
        load_db = False
    elif args.load_db:
        load_db = True
    else:
        load_db = None

    try:
        trade_date = date.fromisoformat(args.trade_date) if args.trade_date else None
        result = run_normalize(
            input_dir=args.input_dir,
            output_dir=args.output_dir,
            write_parquet=not args.no_parquet,
            load_db=load_db,
            trade_date=trade_date,
            replace=trade_date is None,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except (NormalizationError, ValueError) as exc:
        logger.error("%s", exc)
        return 1

    summary: dict[str, Any] = {
        "normalized_at": datetime.now(timezone.utc).isoformat(),
        "broker_rows": result.broker_rows,
        "desk_rows": result.desk_rows,
        "normalized_rows": result.normalized_rows,
        "parquet_path": str(result.parquet_path),
        "canonical_columns": list(CANONICAL_COLUMNS),
        "db_loaded": result.db_loaded,
        "db_counts": result.db_counts,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
