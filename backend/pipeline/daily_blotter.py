"""Daily blotter: one US session of synthetic trades, append/upsert by date, rematch.

Does not call an LLM. Live Massive fetch is optional (skipped without an API key;
generation always reads the Parquet cache / S3).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from backend.data.fetch_market_data import (
    MASSIVE_API_KEY_SSM,
    default_cache_dir,
    download_cache_from_s3,
    load_config,
    resolve_api_key,
    resolve_api_key_from_ssm,
    run_fetch,
    upload_cache_to_s3,
)
from backend.data.eod_prev_backfill import backfill_last_weekday_from_prev
from backend.data.generator import (
    GeneratorConfig,
    closed_market_dates,
    default_output_dir,
    delete_generated_trade_files,
    last_completed_us_session,
    load_market_cache,
    parse_iso_date,
    prior_us_sessions,
    run_generate,
    should_delete_generated_after_db,
)
from backend.db.session import database_url_from_env
from backend.pipeline.ingest import run_normalize
from backend.pipeline.matcher import run_match

logger = logging.getLogger(__name__)

DEFAULT_DAILY_N_TRADES = 40
DEFAULT_LOOKBACK_DAYS = 5
DEFAULT_BACKFILL_SESSIONS = 20


@dataclass
class DailyBlotterResult:
    trade_dates: list[str]
    skipped: list[str] = field(default_factory=list)
    fetch: dict[str, Any] | None = None
    generate: list[dict[str, Any]] = field(default_factory=list)
    match_count: int = 0
    break_count: int = 0
    db_loaded: bool = False
    notes: list[str] = field(default_factory=list)


def _maybe_sync_cache_from_s3(cache_dir: Path) -> list[str]:
    bucket = (os.environ.get("S3_CACHE_BUCKET") or "").strip()
    if not bucket:
        return []
    prefix = (os.environ.get("S3_CACHE_PREFIX") or "market-data").strip()
    region = (
        (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    )
    logger.info("Syncing market-data cache from s3://%s/%s", bucket, prefix)
    return download_cache_from_s3(cache_dir, bucket, prefix, region=region)


def _resolve_massive_key() -> str | None:
    key = resolve_api_key()
    if key:
        return key
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    )
    return resolve_api_key_from_ssm(parameter_name=MASSIVE_API_KEY_SSM, region=region)


def _maybe_fetch(*, cache_dir: Path, lookback_days: int, skip_fetch: bool) -> dict[str, Any] | None:
    if skip_fetch:
        return {"skipped": True, "reason": "skip_fetch"}
    key = _resolve_massive_key()
    if not key:
        logger.info(
            "No MASSIVE_API_KEY / SSM %s — generating from cache/S3 only",
            MASSIVE_API_KEY_SSM,
        )
        return {"skipped": True, "reason": "no_api_key"}
    os.environ.setdefault("MASSIVE_API_KEY", key)
    config = load_config(
        cache_dir=cache_dir,
        lookback_days=lookback_days,
        skip_yfinance=True,
        incremental=True,
        force=False,
    )
    summary = run_fetch(config)
    prev_filled = backfill_last_weekday_from_prev(cache_dir)
    if prev_filled:
        summary = {**summary, "prev_backfill": prev_filled}
    bucket = (os.environ.get("S3_CACHE_BUCKET") or "").strip()
    if bucket:
        prefix = (os.environ.get("S3_CACHE_PREFIX") or "market-data").strip()
        region = (
            os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        )
        uploaded = upload_cache_to_s3(cache_dir, bucket, prefix, region=region)
        summary = {**summary, "s3_uploaded": uploaded}
    return summary


def run_one_session(
    trade_date: date,
    *,
    cache_dir: Path,
    n_trades: int,
    seed: int,
    load_db: bool | None,
    write_parquet: bool,
) -> dict[str, Any]:
    cache = load_market_cache(cache_dir)
    closed = closed_market_dates(cache.calendar)
    if trade_date.weekday() >= 5 or trade_date in closed:
        return {"skipped": True, "trade_date": trade_date.isoformat(), "reason": "closed"}
    cfg = GeneratorConfig(
        cache_dir=cache_dir,
        output_dir=default_output_dir(),
        n_trades=n_trades,
        seed=seed,
        trade_date=trade_date,
        max_corporate_action_breaks=3,
    )
    generated = run_generate(cfg)
    url = database_url_from_env()
    should_load = load_db if load_db is not None else bool(url)
    norm = run_normalize(
        database_url=url,
        write_parquet=write_parquet,
        load_db=should_load,
        trade_date=trade_date,
        replace=False,
    )
    return {
        "skipped": False,
        "trade_date": trade_date.isoformat(),
        "generate": generated,
        "normalized_rows": norm.normalized_rows,
        "db_loaded": bool(norm.db_loaded),
        "parquet_dir": str(norm.parquet_path.parent),
    }


def run_daily_blotter(
    *,
    trade_date: date | None = None,
    backfill_sessions: int = 1,
    n_trades: int = DEFAULT_DAILY_N_TRADES,
    seed: int = 42,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    skip_fetch: bool = False,
    skip_s3_sync: bool = False,
    cache_dir: Path | None = None,
    load_db: bool | None = None,
    write_parquet: bool = True,
    as_of: date | None = None,
) -> DailyBlotterResult:
    """Generate + ingest one or more sessions, then rematch the full book each day."""
    cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    notes: list[str] = []
    if not skip_s3_sync:
        try:
            pulled = _maybe_sync_cache_from_s3(cache)
            notes.append(f"s3_downloaded={len(pulled)}")
        except Exception as exc:  # noqa: BLE001 — cache may already be local
            logger.warning("S3 cache sync failed (%s); using local cache", type(exc).__name__)
            notes.append("s3_sync_failed")

    fetch_summary = _maybe_fetch(
        cache_dir=cache, lookback_days=lookback_days, skip_fetch=skip_fetch
    )

    market = load_market_cache(cache)
    closed = closed_market_dates(market.calendar)
    today = as_of or date.today()

    if trade_date is not None:
        sessions = [trade_date]
    else:
        n = max(1, int(backfill_sessions))
        sessions = prior_us_sessions(n, as_of=today, closed=closed)
        if n == 1:
            sessions = [last_completed_us_session(today, closed)]

    result = DailyBlotterResult(
        trade_dates=[d.isoformat() for d in sessions],
        fetch=fetch_summary,
        notes=notes,
    )
    last_match: dict[str, Any] | None = None
    for session_day in sessions:
        one = run_one_session(
            session_day,
            cache_dir=cache,
            n_trades=n_trades,
            seed=seed,
            load_db=load_db,
            write_parquet=write_parquet,
        )
        if one.get("skipped"):
            result.skipped.append(str(one.get("trade_date")))
            continue
        result.generate.append(
            {
                "trade_date": one["trade_date"],
                "n_broker_rows": one["generate"].get("n_broker_rows"),
                "n_desk_rows": one["generate"].get("n_desk_rows"),
            }
        )
        last_match = one
        result.db_loaded = result.db_loaded or bool(one.get("db_loaded"))
    if last_match:
        url = database_url_from_env()
        should_load = load_db if load_db is not None else bool(url)
        match = run_match(
            normalized_dir=Path(str(last_match.get("parquet_dir")))
            if last_match.get("parquet_dir")
            else None,
            cache_dir=cache,
            database_url=url,
            write_parquet=write_parquet,
            load_db=should_load,
            from_db=should_load,
        )
        result.match_count = match.match_rows
        result.break_count = match.break_rows
        result.db_loaded = result.db_loaded or bool(match.db_loaded)
    if result.db_loaded and should_delete_generated_after_db():
        dropped = delete_generated_trade_files(default_output_dir())
        notes.append(f"deleted_generated={len(dropped)}")
        logger.info("Removed %d generated blotter file(s) after RDS ingest", len(dropped))
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Daily blotter: optional incremental Massive fetch, generate one "
            "US session (idempotent seed), replace that date in DB, rematch full book."
        )
    )
    parser.add_argument("--trade-date", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument(
        "--backfill-sessions",
        type=int,
        default=1,
        help=f"When --trade-date is omitted, generate the last N sessions (default 1; use {DEFAULT_BACKFILL_SESSIONS} once)",
    )
    parser.add_argument("--n-trades", type=int, default=DEFAULT_DAILY_N_TRADES)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-s3-sync", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument("--no-parquet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_db = False if args.no_db else None
    try:
        td = parse_iso_date(args.trade_date) if args.trade_date else None
        result = run_daily_blotter(
            trade_date=td,
            backfill_sessions=args.backfill_sessions,
            n_trades=args.n_trades,
            seed=args.seed,
            lookback_days=args.lookback_days,
            skip_fetch=args.skip_fetch,
            skip_s3_sync=args.skip_s3_sync,
            cache_dir=args.cache_dir,
            load_db=load_db,
            write_parquet=not args.no_parquet,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    payload = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "trade_dates": result.trade_dates,
        "skipped": result.skipped,
        "fetch": result.fetch,
        "generate": result.generate,
        "match_count": result.match_count,
        "break_count": result.break_count,
        "db_loaded": result.db_loaded,
        "notes": result.notes,
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
