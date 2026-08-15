"""Backfill the last weekday session from Massive /prev when range aggs lag.

Used by daily blotter on EC2. Never logs the API key.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from backend.data.fetch_market_data import (
    BARS_COLUMNS,
    bar_path,
    cached_bar_max_date,
    default_cache_dir,
    merge_bar_frames,
    read_parquet,
    resolve_api_key,
    write_parquet,
)

logger = logging.getLogger(__name__)


def last_weekday_on_or_before(as_of: date) -> date:
    current = as_of
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def _session_dates_from_ts_ms(ts_ms: int) -> set[date]:
    utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return {utc.date(), utc.astimezone(ZoneInfo("America/New_York")).date()}


def backfill_last_weekday_from_prev(
    cache_dir: Path | None = None,
    *,
    session: date | None = None,
    delay_seconds: float = 12.0,
) -> list[str]:
    cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    target = session or last_weekday_on_or_before(date.today())
    key = resolve_api_key()
    if not key:
        logger.info("No MASSIVE_API_KEY — skip previous-close backfill")
        return []
    bars_dir = cache / "bars"
    if not bars_dir.is_dir():
        return []
    symbols = sorted(p.stem.upper() for p in bars_dir.glob("*.parquet") if p.stem)
    missing = [
        symbol
        for symbol in symbols
        if (cached_bar_max_date(cache, symbol) or date.min) < target
    ]
    if not missing:
        return []
    filled: list[str] = []
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=30.0) as client:
        for i, symbol in enumerate(missing):
            if i > 0 and delay_seconds > 0:
                time.sleep(delay_seconds)
            url = f"https://api.massive.com/v2/aggs/ticker/{symbol}/prev"
            try:
                resp = client.get(url, params={"adjusted": "true"}, headers=headers)
                if resp.status_code in {429, 503}:
                    time.sleep(12.0)
                    resp = client.get(url, params={"adjusted": "true"}, headers=headers)
                resp.raise_for_status()
                results = (resp.json() or {}).get("results") or []
            except Exception as exc:  # noqa: BLE001
                logger.warning("previous-close failed for %s: %s", symbol, type(exc).__name__)
                continue
            if not results:
                continue
            bar = results[0]
            ts_ms = bar.get("t")
            if not isinstance(ts_ms, (int, float)):
                continue
            if target not in _session_dates_from_ts_ms(int(ts_ms)):
                continue
            incoming = pd.DataFrame(
                [
                    {
                        "ticker": symbol,
                        "date": target.isoformat(),
                        "open": bar.get("o"),
                        "high": bar.get("h"),
                        "low": bar.get("l"),
                        "close": bar.get("c"),
                        "volume": bar.get("v"),
                        "vwap": bar.get("vw"),
                        "transactions": bar.get("n"),
                    }
                ],
                columns=list(BARS_COLUMNS),
            )
            path = bar_path(cache, symbol)
            existing = (
                read_parquet(path) if path.is_file() else pd.DataFrame(columns=list(BARS_COLUMNS))
            )
            write_parquet(merge_bar_frames(existing, incoming), path)
            filled.append(symbol)
    logger.info("previous-close backfill %s filled %d symbols", target.isoformat(), len(filled))
    return filled


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print({"filled": backfill_last_weekday_from_prev()})
