"""Fetch market data from Massive (primary) and cache as Parquet.

Cross-checks split dates against yfinance (spot-check only — Massive is
source of record). Pipeline and agent code must read from the cache only;
they must never call this module's live HTTP paths.

Usage:
    uv run python -m backend.data.fetch_market_data
    uv run fetch-market-data

Env:
    MASSIVE_API_KEY or POLYGON_API_KEY  (required for live fetch)
    MARKET_DATA_CACHE_DIR               (default: backend/data/cache)
    MARKET_DATA_LOOKBACK_DAYS           (default: 730 ≈ Massive Basic history)
    S3_CACHE_BUCKET / S3_CACHE_PREFIX   (optional upload after local write)
    AWS_REGION or AWS_DEFAULT_REGION    (S3 client region; default us-east-1)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

MASSIVE_BASE_URL = "https://api.massive.com"
# Status codes that warrant a retry (rate limit / transient overload).
_RETRYABLE_STATUS_CODES = frozenset({429, 503})
_DEFAULT_MAX_RETRIES = 8
# Free-tier Massive/Polygon is ~5 req/min — wait a full window before retrying.
_DEFAULT_BASE_BACKOFF_SECONDS = 12.0
# Pause between per-symbol bar fetches to stay under free-tier rate limits.
_DEFAULT_SYMBOL_DELAY_SECONDS = 12.0

# Provisional starter universe (~40 liquid US equities). Pinned in
# project_plan.md §10 as the default for step 1; revisit before demo.
DEFAULT_SYMBOLS: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "META",
    "NVDA",
    "TSLA",
    "JPM",
    "V",
    "MA",
    "UNH",
    "XOM",
    "JNJ",
    "WMT",
    "PG",
    "HD",
    "CVX",
    "MRK",
    "ABBV",
    "KO",
    "PEP",
    "COST",
    "AVGO",
    "AMD",
    "CRM",
    "NFLX",
    "ADBE",
    "ORCL",
    "INTC",
    "QCOM",
    "IBM",
    "BA",
    "CAT",
    "GS",
    "MS",
    "BAC",
    "WFC",
    "DIS",
    "NKE",
    "MCD",
)

BARS_COLUMNS: tuple[str, ...] = (
    "ticker",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "transactions",
)
SPLITS_COLUMNS: tuple[str, ...] = (
    "ticker",
    "execution_date",
    "split_from",
    "split_to",
    "adjustment_type",
    "historical_adjustment_factor",
    "id",
)
DIVIDENDS_COLUMNS: tuple[str, ...] = (
    "ticker",
    "ex_dividend_date",
    "pay_date",
    "record_date",
    "declaration_date",
    "cash_amount",
    "split_adjusted_cash_amount",
    "frequency",
    "distribution_type",
    "historical_adjustment_factor",
    "id",
)
CALENDAR_COLUMNS: tuple[str, ...] = (
    "date",
    "exchange",
    "name",
    "status",
    "open",
    "close",
)


class HttpClient(Protocol):
    """Minimal HTTP client protocol for dependency injection / mocking."""

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response: ...


@dataclass
class FetchConfig:
    """Runtime config for a market-data fetch run."""

    api_key: str
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    cache_dir: Path = field(default_factory=lambda: Path("backend/data/cache"))
    lookback_days: int = 730
    base_url: str = MASSIVE_BASE_URL
    s3_bucket: str | None = None
    s3_prefix: str = "market-data"
    aws_region: str = "us-east-1"
    skip_yfinance: bool = False
    request_timeout: float = 30.0
    force: bool = False
    incremental: bool = True
    skip_cached: bool = False
    symbol_delay_seconds: float = _DEFAULT_SYMBOL_DELAY_SECONDS
    max_retries: int = _DEFAULT_MAX_RETRIES
    base_backoff_seconds: float = _DEFAULT_BASE_BACKOFF_SECONDS


def resolve_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """Return MASSIVE_API_KEY or POLYGON_API_KEY from env, if set."""
    source = env if env is not None else os.environ
    for name in ("MASSIVE_API_KEY", "POLYGON_API_KEY"):
        value = source.get(name, "").strip()
        if value:
            return value
    return None


def default_cache_dir() -> Path:
    """Resolve cache dir from env or project-relative default."""
    override = os.environ.get("MARKET_DATA_CACHE_DIR", "").strip()
    if override:
        return Path(override)
    return Path("backend/data/cache")


def load_config(
    *,
    symbols: tuple[str, ...] | None = None,
    cache_dir: Path | None = None,
    lookback_days: int | None = None,
    skip_yfinance: bool = False,
    force: bool = False,
    incremental: bool = True,
    skip_cached: bool = False,
    env: Mapping[str, str] | None = None,
) -> FetchConfig:
    """Build FetchConfig from env + optional CLI overrides."""
    source = env if env is not None else os.environ
    api_key = resolve_api_key(source)
    if not api_key:
        raise ValueError(
            "Set MASSIVE_API_KEY or POLYGON_API_KEY in the environment "
            "(see .env.example)."
        )
    days = lookback_days
    if days is None:
        raw = source.get("MARKET_DATA_LOOKBACK_DAYS", "").strip()
        days = int(raw) if raw else 730
    bucket = (source.get("S3_CACHE_BUCKET") or "").strip() or None
    prefix = (source.get("S3_CACHE_PREFIX") or "market-data").strip() or "market-data"
    # Prefer explicit project/env region; fall back to AWS_DEFAULT_REGION.
    region = (
        (source.get("AWS_REGION") or "").strip()
        or (source.get("AWS_DEFAULT_REGION") or "").strip()
        or "us-east-1"
    )
    return FetchConfig(
        api_key=api_key,
        symbols=symbols or DEFAULT_SYMBOLS,
        cache_dir=cache_dir or default_cache_dir(),
        lookback_days=days,
        s3_bucket=bucket,
        s3_prefix=prefix,
        aws_region=region,
        skip_yfinance=skip_yfinance,
        force=force,
        incremental=incremental,
        skip_cached=skip_cached,
    )


def redact_url_for_log(url: str) -> str:
    """Return URL without query string so apiKey never appears in logs."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_retry_after(value: str | None, *, fallback: float) -> float:
    """Parse Retry-After header (seconds); fall back on missing/invalid."""
    if value is None or not str(value).strip():
        return fallback
    try:
        return max(0.0, float(str(value).strip()))
    except ValueError:
        return fallback


def cached_bars_nonempty(cache_dir: Path, symbol: str) -> bool:
    """True if bars/{SYMBOL}.parquet exists and has at least one row."""
    path = bar_path(cache_dir, symbol)
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        df = read_parquet(path)
    except Exception as exc:  # noqa: BLE001 — corrupt cache → re-fetch
        logger.warning("Could not read cached bars for %s (%s); will re-fetch", symbol, exc)
        return False
    return not df.empty


def _bar_dates(df: pd.DataFrame) -> list[date]:
    if df.empty or "date" not in df.columns:
        return []
    return list(pd.to_datetime(df["date"]).dt.date)


def cached_bar_max_date(cache_dir: Path, symbol: str) -> date | None:
    """Latest session date in ``bars/{SYMBOL}.parquet``, if any."""
    path = bar_path(cache_dir, symbol)
    if not path.is_file():
        return None
    try:
        df = read_parquet(path)
    except Exception:  # noqa: BLE001
        return None
    dates = _bar_dates(df)
    return max(dates) if dates else None


def merge_bar_frames(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    """Union bar frames; later rows win on ``(ticker, date)``."""
    frames = [f for f in (existing, incoming) if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame(columns=list(BARS_COLUMNS))
    combined = pd.concat(frames, ignore_index=True)
    if "date" in combined.columns:
        combined = combined.copy()
        combined["_d"] = pd.to_datetime(combined["date"]).dt.date
        combined = combined.drop_duplicates(subset=["ticker", "_d"], keep="last")
        combined = combined.drop(columns=["_d"])
    cols = [c for c in BARS_COLUMNS if c in combined.columns]
    return combined[cols].reset_index(drop=True)


MASSIVE_API_KEY_SSM = "/trade-recon/massive-api-key"


def resolve_api_key_from_ssm(
    *,
    parameter_name: str = MASSIVE_API_KEY_SSM,
    region: str = "us-east-1",
    ssm_client: Any | None = None,
) -> str | None:
    """Read Massive key from SSM SecureString. Never logs the value."""
    try:
        import boto3  # noqa: PLC0415
    except ImportError:
        return None
    client = ssm_client or boto3.client("ssm", region_name=region)
    try:
        resp = client.get_parameter(Name=parameter_name, WithDecryption=True)
    except Exception as exc:  # noqa: BLE001 — missing param is normal
        logger.info("SSM %s not available (%s)", parameter_name, type(exc).__name__)
        return None
    value = str((resp.get("Parameter") or {}).get("Value") or "").strip()
    return value or None


def download_cache_from_s3(
    cache_dir: Path,
    bucket: str,
    prefix: str,
    *,
    region: str = "us-east-1",
    s3_client: Any | None = None,
) -> list[str]:
    """Download Parquet cache from s3://bucket/prefix into cache_dir."""
    if s3_client is None:
        import boto3  # noqa: PLC0415

        s3_client = boto3.client("s3", region_name=region)
    cache_dir.mkdir(parents=True, exist_ok=True)
    prefix_norm = prefix.rstrip("/") + "/"
    downloaded: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix_norm):
        for obj in page.get("Contents") or []:
            key = str(obj.get("Key") or "")
            if not key or key.endswith("/"):
                continue
            relative = key[len(prefix_norm) :] if key.startswith(prefix_norm) else key
            dest = cache_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3_client.download_file(bucket, key, str(dest))
            downloaded.append(f"s3://{bucket}/{key}")
    return downloaded


class MassiveClient:
    """Thin Massive (api.massive.com) REST client. Polygon keys still work."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = MASSIVE_BASE_URL,
        client: HttpClient | None = None,
        timeout: float = 30.0,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        base_backoff_seconds: float = _DEFAULT_BASE_BACKOFF_SECONDS,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client: HttpClient = client or httpx.Client(timeout=timeout)
        self.max_retries = max_retries
        self.base_backoff_seconds = base_backoff_seconds
        self._sleep: Callable[[float], None] = sleep or time.sleep

    def close(self) -> None:
        if self._owns_client and hasattr(self._client, "close"):
            self._client.close()  # type: ignore[attr-defined]

    def __enter__(self) -> MassiveClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _auth_headers(self) -> dict[str, str]:
        # Prefer Bearer so the key is not embedded in request URLs (httpx logs).
        return {"Authorization": f"Bearer {self.api_key}"}

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        # Do not put apiKey in the query string — keeps logs/redacted URLs clean.
        query = dict(params or {})
        query.pop("apiKey", None)
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        safe_url = redact_url_for_log(url)
        last_status: int | None = None

        for attempt in range(self.max_retries + 1):
            response = self._client.get(
                url,
                params=query or None,
                headers=self._auth_headers(),
            )
            status = getattr(response, "status_code", 200)
            last_status = status
            if status in _RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                headers = getattr(response, "headers", None) or {}
                retry_after = None
                if hasattr(headers, "get"):
                    retry_after = headers.get("Retry-After") or headers.get(
                        "retry-after"
                    )
                fallback = self.base_backoff_seconds * (2**attempt)
                sleep_s = parse_retry_after(
                    str(retry_after) if retry_after is not None else None,
                    fallback=fallback,
                )
                logger.warning(
                    "HTTP %s for %s; retry %d/%d after %.1fs",
                    status,
                    safe_url,
                    attempt + 1,
                    self.max_retries,
                    sleep_s,
                )
                self._sleep(sleep_s)
                continue
            response.raise_for_status()
            return response.json()

        raise httpx.HTTPStatusError(
            f"Exhausted retries after HTTP {last_status} for {safe_url}",
            request=httpx.Request("GET", safe_url),
            response=httpx.Response(last_status or 429),
        )

    def _paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        results_key: str = "results",
    ) -> list[dict[str, Any]]:
        """Follow next_url until exhausted; return flattened results."""
        collected: list[dict[str, Any]] = []
        data = self._get(path, params)
        while True:
            if isinstance(data, list):
                collected.extend(data)
                break
            batch = data.get(results_key) or []
            collected.extend(batch)
            next_url = data.get("next_url")
            if not next_url:
                break
            # next_url may already include apiKey; still pass ours for safety
            data = self._get(next_url, {})
        return collected

    def fetch_daily_bars(
        self,
        ticker: str,
        start: date,
        end: date,
        *,
        adjusted: bool = True,
    ) -> list[dict[str, Any]]:
        path = (
            f"/v2/aggs/ticker/{ticker}/range/1/day/"
            f"{start.isoformat()}/{end.isoformat()}"
        )
        return self._paginate(
            path,
            {"adjusted": str(adjusted).lower(), "sort": "asc", "limit": 50000},
        )

    def fetch_grouped_daily(
        self,
        session: date,
        *,
        adjusted: bool = True,
    ) -> list[dict[str, Any]]:
        """Official US grouped daily file for one session (often ahead of per-ticker range)."""
        path = f"/v2/aggs/grouped/locale/us/market/stocks/{session.isoformat()}"
        try:
            data = self._get(path, {"adjusted": str(adjusted).lower()})
        except httpx.HTTPStatusError as exc:
            status = getattr(exc.response, "status_code", None)
            if status in {404, 403, 429}:
                logger.warning(
                    "Grouped daily %s not available (HTTP %s)",
                    session.isoformat(),
                    status,
                )
                return []
            raise
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        return list(data.get("results") or [])

    def fetch_previous_close(
        self,
        ticker: str,
        *,
        adjusted: bool = True,
    ) -> list[dict[str, Any]]:
        path = f"/v2/aggs/ticker/{ticker}/prev"
        try:
            data = self._get(path, {"adjusted": str(adjusted).lower()})
        except httpx.HTTPStatusError as exc:
            status = getattr(exc.response, "status_code", None)
            if status in {404, 403, 429}:
                logger.warning(
                    "Previous close for %s not available (HTTP %s)", ticker, status
                )
                return []
            raise
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        return list(data.get("results") or [])

    def fetch_splits(
        self,
        tickers: tuple[str, ...],
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        return self._paginate(
            "/stocks/v1/splits",
            {
                "ticker.any_of": ",".join(tickers),
                "execution_date.gte": start.isoformat(),
                "execution_date.lte": end.isoformat(),
                "limit": 1000,
                "sort": "execution_date.asc",
            },
        )

    def fetch_dividends(
        self,
        tickers: tuple[str, ...],
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        return self._paginate(
            "/stocks/v1/dividends",
            {
                "ticker.any_of": ",".join(tickers),
                "ex_dividend_date.gte": start.isoformat(),
                "ex_dividend_date.lte": end.isoformat(),
                "limit": 1000,
                "sort": "ex_dividend_date.asc",
            },
        )

    def fetch_market_holidays(self) -> list[dict[str, Any]]:
        """Upcoming holidays / early closes (Massive forward-looking calendar)."""
        data = self._get("/v1/marketstatus/upcoming")
        if isinstance(data, list):
            return data
        return list(data.get("results") or [])


def last_weekday_on_or_before(as_of: date) -> date:
    """Most recent Mon–Fri calendar day on or before ``as_of`` (not holiday-aware)."""
    current = as_of
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def _session_dates_from_ts_ms(ts_ms: int) -> set[date]:
    utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    dates = {utc.date()}
    dates.add(utc.astimezone(ZoneInfo("America/New_York")).date())
    return dates


def grouped_bars_to_frames(
    raw: list[dict[str, Any]],
    session: date,
    symbols: tuple[str, ...],
) -> dict[str, pd.DataFrame]:
    """Map grouped-daily results onto ``session`` (path date), not the bar timestamp."""
    wanted = {s.upper() for s in symbols}
    rows_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for bar in raw:
        ticker = str(bar.get("T") or bar.get("ticker") or "").upper()
        if ticker not in wanted:
            continue
        rows_by_ticker.setdefault(ticker, []).append(
            {
                "ticker": ticker,
                "date": session.isoformat(),
                "open": bar.get("o"),
                "high": bar.get("h"),
                "low": bar.get("l"),
                "close": bar.get("c"),
                "volume": bar.get("v"),
                "vwap": bar.get("vw"),
                "transactions": bar.get("n"),
            }
        )
    return {
        ticker: pd.DataFrame(rows, columns=list(BARS_COLUMNS))
        for ticker, rows in rows_by_ticker.items()
    }


def bars_to_dataframe(ticker: str, raw: list[dict[str, Any]]) -> pd.DataFrame:
    """Normalize Massive aggregate bars into a typed DataFrame."""
    if not raw:
        return pd.DataFrame(columns=list(BARS_COLUMNS))
    rows = []
    for bar in raw:
        ts_ms = bar.get("t")
        if ts_ms is None:
            continue
        bar_date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
        rows.append(
            {
                "ticker": ticker,
                "date": bar_date.isoformat(),
                "open": bar.get("o"),
                "high": bar.get("h"),
                "low": bar.get("l"),
                "close": bar.get("c"),
                "volume": bar.get("v"),
                "vwap": bar.get("vw"),
                "transactions": bar.get("n"),
            }
        )
    df = pd.DataFrame(rows, columns=list(BARS_COLUMNS))
    return df


def splits_to_dataframe(raw: list[dict[str, Any]]) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=list(SPLITS_COLUMNS))
    rows = [
        {
            "ticker": r.get("ticker"),
            "execution_date": r.get("execution_date"),
            "split_from": r.get("split_from"),
            "split_to": r.get("split_to"),
            "adjustment_type": r.get("adjustment_type"),
            "historical_adjustment_factor": r.get("historical_adjustment_factor"),
            "id": r.get("id"),
        }
        for r in raw
    ]
    return pd.DataFrame(rows, columns=list(SPLITS_COLUMNS))


def dividends_to_dataframe(raw: list[dict[str, Any]]) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=list(DIVIDENDS_COLUMNS))
    rows = [
        {
            "ticker": r.get("ticker"),
            "ex_dividend_date": r.get("ex_dividend_date"),
            "pay_date": r.get("pay_date"),
            "record_date": r.get("record_date"),
            "declaration_date": r.get("declaration_date"),
            "cash_amount": r.get("cash_amount"),
            "split_adjusted_cash_amount": r.get("split_adjusted_cash_amount"),
            "frequency": r.get("frequency"),
            "distribution_type": r.get("distribution_type"),
            "historical_adjustment_factor": r.get("historical_adjustment_factor"),
            "id": r.get("id"),
        }
        for r in raw
    ]
    return pd.DataFrame(rows, columns=list(DIVIDENDS_COLUMNS))


def calendar_to_dataframe(raw: list[dict[str, Any]]) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=list(CALENDAR_COLUMNS))
    rows = [
        {
            "date": r.get("date"),
            "exchange": r.get("exchange"),
            "name": r.get("name"),
            "status": r.get("status"),
            "open": r.get("open"),
            "close": r.get("close"),
        }
        for r in raw
    ]
    return pd.DataFrame(rows, columns=list(CALENDAR_COLUMNS))


def ensure_cache_layout(cache_dir: Path) -> dict[str, Path]:
    """Create cache directories; return key paths."""
    bars_dir = cache_dir / "bars"
    cross_check_dir = cache_dir / "cross_check"
    bars_dir.mkdir(parents=True, exist_ok=True)
    cross_check_dir.mkdir(parents=True, exist_ok=True)
    return {
        "root": cache_dir,
        "bars": bars_dir,
        "splits": cache_dir / "splits.parquet",
        "dividends": cache_dir / "dividends.parquet",
        "calendar": cache_dir / "calendar.parquet",
        "cross_check_dir": cross_check_dir,
        "cross_check_json": cross_check_dir / "splits_report.json",
        "cross_check_csv": cross_check_dir / "splits_report.csv",
    }


def write_parquet(df: pd.DataFrame, path: Path) -> Path:
    """Write DataFrame to Parquet; create parent dirs if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path


def read_parquet(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path)


def bar_path(cache_dir: Path, symbol: str) -> Path:
    return cache_dir / "bars" / f"{symbol.upper()}.parquet"


def fetch_yfinance_splits(
    symbols: tuple[str, ...],
    *,
    yf_module: Any | None = None,
) -> pd.DataFrame:
    """Fetch split events from yfinance for cross-check (spot-check only)."""
    yf = yf_module
    if yf is None:
        import yfinance as yf  # noqa: PLC0415 — optional live dependency

    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            splits = ticker.splits
        except Exception as exc:  # noqa: BLE001 — isolate per-symbol failures
            logger.warning("yfinance split fetch failed for %s: %s", symbol, exc)
            continue
        if splits is None or len(splits) == 0:
            continue
        for idx, ratio in splits.items():
            split_date = pd.Timestamp(idx).date().isoformat()
            rows.append(
                {
                    "ticker": symbol,
                    "execution_date": split_date,
                    "split_ratio": float(ratio),
                    "source": "yfinance",
                }
            )
    if not rows:
        return pd.DataFrame(
            columns=["ticker", "execution_date", "split_ratio", "source"]
        )
    return pd.DataFrame(rows)


def _massive_split_ratio(row: Mapping[str, Any]) -> float | None:
    split_from = row.get("split_from")
    split_to = row.get("split_to")
    if split_from in (None, 0) or split_to is None:
        return None
    try:
        return float(split_to) / float(split_from)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def cross_check_splits(
    massive_splits: pd.DataFrame,
    yfinance_splits: pd.DataFrame,
    *,
    date_tolerance_days: int = 0,
) -> pd.DataFrame:
    """Compare Massive vs yfinance split dates per ticker.

    Returns a report DataFrame with one row per (ticker, date) discrepancy
    or match summary row types: match | missing_in_yfinance |
    missing_in_massive | ratio_mismatch.
    """
    report_rows: list[dict[str, Any]] = []

    massive = massive_splits.copy()
    yf_df = yfinance_splits.copy()

    if massive.empty and yf_df.empty:
        return pd.DataFrame(
            columns=[
                "ticker",
                "status",
                "massive_date",
                "yfinance_date",
                "massive_ratio",
                "yfinance_ratio",
                "detail",
            ]
        )

    if not massive.empty:
        massive["_ratio"] = massive.apply(_massive_split_ratio, axis=1)
        massive["_date"] = pd.to_datetime(massive["execution_date"]).dt.date
    if not yf_df.empty:
        yf_df["_date"] = pd.to_datetime(yf_df["execution_date"]).dt.date

    tickers = sorted(
        set(massive["ticker"].dropna().unique() if not massive.empty else [])
        | set(yf_df["ticker"].dropna().unique() if not yf_df.empty else [])
    )

    for ticker in tickers:
        m_rows = (
            massive[massive["ticker"] == ticker]
            if not massive.empty
            else pd.DataFrame()
        )
        y_rows = (
            yf_df[yf_df["ticker"] == ticker] if not yf_df.empty else pd.DataFrame()
        )
        m_dates = {
            d: r
            for d, r in zip(
                m_rows["_date"].tolist() if not m_rows.empty else [],
                m_rows.to_dict("records") if not m_rows.empty else [],
                strict=True,
            )
        }
        y_dates = {
            d: r
            for d, r in zip(
                y_rows["_date"].tolist() if not y_rows.empty else [],
                y_rows.to_dict("records") if not y_rows.empty else [],
                strict=True,
            )
        }

        matched_y: set[date] = set()
        for m_date, m_row in m_dates.items():
            partner: date | None = None
            for y_date in y_dates:
                if abs((m_date - y_date).days) <= date_tolerance_days:
                    partner = y_date
                    break
            m_ratio = m_row.get("_ratio")
            if partner is None:
                report_rows.append(
                    {
                        "ticker": ticker,
                        "status": "missing_in_yfinance",
                        "massive_date": m_date.isoformat(),
                        "yfinance_date": None,
                        "massive_ratio": m_ratio,
                        "yfinance_ratio": None,
                        "detail": "Present in Massive, not found in yfinance",
                    }
                )
                continue
            matched_y.add(partner)
            y_ratio = y_dates[partner].get("split_ratio")
            ratio_ok = (
                m_ratio is not None
                and y_ratio is not None
                and abs(float(m_ratio) - float(y_ratio)) < 1e-6
            )
            report_rows.append(
                {
                    "ticker": ticker,
                    "status": "match" if ratio_ok else "ratio_mismatch",
                    "massive_date": m_date.isoformat(),
                    "yfinance_date": partner.isoformat(),
                    "massive_ratio": m_ratio,
                    "yfinance_ratio": y_ratio,
                    "detail": (
                        "Dates and ratios agree"
                        if ratio_ok
                        else "Dates align but split ratios differ"
                    ),
                }
            )

        for y_date, y_row in y_dates.items():
            if y_date in matched_y:
                continue
            # Already covered by tolerance match above
            already = any(
                abs((y_date - m_date).days) <= date_tolerance_days for m_date in m_dates
            )
            if already:
                continue
            report_rows.append(
                {
                    "ticker": ticker,
                    "status": "missing_in_massive",
                    "massive_date": None,
                    "yfinance_date": y_date.isoformat(),
                    "massive_ratio": None,
                    "yfinance_ratio": y_row.get("split_ratio"),
                    "detail": "Present in yfinance, not found in Massive",
                }
            )

    return pd.DataFrame(report_rows)


def write_cross_check_report(
    report: pd.DataFrame,
    paths: Mapping[str, Path],
) -> dict[str, Path]:
    """Write JSON + CSV cross-check artifacts."""
    json_path = paths["cross_check_json"]
    csv_path = paths["cross_check_csv"]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_of_record": "massive",
        "cross_check_source": "yfinance",
        "summary": {
            "total_rows": int(len(report)),
            "matches": int((report["status"] == "match").sum())
            if not report.empty
            else 0,
            "mismatches": int((report["status"] != "match").sum())
            if not report.empty
            else 0,
        },
        "rows": report.to_dict(orient="records") if not report.empty else [],
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    report.to_csv(csv_path, index=False)
    return {"json": json_path, "csv": csv_path}


def _write_merged_bars(
    cache_dir: Path,
    symbol: str,
    incoming: pd.DataFrame,
) -> None:
    out_path = bar_path(cache_dir, symbol)
    existing = pd.DataFrame(columns=list(BARS_COLUMNS))
    if out_path.is_file():
        try:
            existing = read_parquet(out_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read %s (%s); treating as empty", out_path, exc)
    merged = merge_bar_frames(existing, incoming)
    write_parquet(merged, out_path)


def _backfill_missing_session(
    config: FetchConfig,
    massive: MassiveClient,
    *,
    session: date,
) -> list[str]:
    """Fill a session from grouped daily / previous-close when range aggs lag."""
    missing = [
        symbol
        for symbol in config.symbols
        if (cached_bar_max_date(config.cache_dir, symbol) or date.min) < session
    ]
    if not missing:
        return []
    logger.info(
        "Range aggs missing session %s for %d symbols; trying previous-close then grouped",
        session.isoformat(),
        len(missing),
    )
    filled: list[str] = []
    still_missing: list[str] = []
    for i, symbol in enumerate(missing):
        if i > 0 and config.symbol_delay_seconds > 0:
            time.sleep(config.symbol_delay_seconds)
        raw_prev = massive.fetch_previous_close(symbol)
        if not raw_prev:
            still_missing.append(symbol)
            continue
        ts_ms = raw_prev[0].get("t")
        if not isinstance(ts_ms, (int, float)) or session not in _session_dates_from_ts_ms(
            int(ts_ms)
        ):
            still_missing.append(symbol)
            continue
        forced = grouped_bars_to_frames(
            [{**raw_prev[0], "T": symbol.upper()}],
            session,
            (symbol.upper(),),
        ).get(symbol.upper())
        if forced is None or forced.empty:
            still_missing.append(symbol)
            continue
        _write_merged_bars(config.cache_dir, symbol, forced)
        filled.append(symbol)
        logger.info("Backfilled %s for %s from previous-close", session.isoformat(), symbol)

    if still_missing:
        raw_grouped = massive.fetch_grouped_daily(session)
        frames = grouped_bars_to_frames(raw_grouped, session, tuple(still_missing))
        leftover: list[str] = []
        for symbol in still_missing:
            frame = frames.get(symbol.upper())
            if frame is None or frame.empty:
                leftover.append(symbol)
                continue
            _write_merged_bars(config.cache_dir, symbol, frame)
            filled.append(symbol)
        still_missing = leftover

    if filled:
        logger.info(
            "Backfilled %s bars for %d symbols from grouped/prev",
            session.isoformat(),
            len(filled),
        )
    else:
        logger.warning(
            "Grouped/prev did not provide session %s (range aggs still lag)",
            session.isoformat(),
        )
    return filled


def upload_cache_to_s3(
    cache_dir: Path,
    bucket: str,
    prefix: str,
    *,
    region: str = "us-east-1",
    s3_client: Any | None = None,
) -> list[str]:
    """Upload all files under cache_dir to s3://bucket/prefix/..."""
    if s3_client is None:
        import boto3  # noqa: PLC0415

        s3_client = boto3.client("s3", region_name=region)

    uploaded: list[str] = []
    for path in sorted(cache_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(cache_dir).as_posix()
        key = f"{prefix.rstrip('/')}/{relative}"
        s3_client.upload_file(str(path), bucket, key)
        uploaded.append(f"s3://{bucket}/{key}")
    return uploaded


def run_fetch(
    config: FetchConfig,
    *,
    client: MassiveClient | None = None,
    yfinance_fetcher: Callable[[tuple[str, ...]], pd.DataFrame] | None = None,
    s3_client: Any | None = None,
    end_date: date | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Fetch Massive data, write Parquet cache, optionally cross-check + S3."""
    end = end_date or date.today()
    start = end - timedelta(days=config.lookback_days)
    paths = ensure_cache_layout(config.cache_dir)
    sleeper = sleep or time.sleep

    owns_client = client is None
    massive = client or MassiveClient(
        config.api_key,
        base_url=config.base_url,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
        base_backoff_seconds=config.base_backoff_seconds,
        sleep=sleeper,
    )

    try:
        bar_files: list[str] = []
        skipped_symbols: list[str] = []
        failed_symbols: list[dict[str, str]] = []
        fetched_count = 0
        incremental_symbols: list[str] = []
        for symbol in config.symbols:
            out_path = bar_path(config.cache_dir, symbol)
            existing = pd.DataFrame(columns=list(BARS_COLUMNS))
            if out_path.is_file():
                try:
                    existing = read_parquet(out_path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not read %s (%s); treating as empty", out_path, exc)
                    existing = pd.DataFrame(columns=list(BARS_COLUMNS))

            max_cached = cached_bar_max_date(config.cache_dir, symbol)
            skip_whole = (
                not config.force
                and config.skip_cached
                and cached_bars_nonempty(config.cache_dir, symbol)
            )
            already_current = (
                not config.force
                and config.incremental
                and max_cached is not None
                and max_cached >= end
            )
            if skip_whole or already_current:
                logger.info(
                    "Skipping %s — cache current through %s at %s",
                    symbol,
                    max_cached.isoformat() if max_cached else "existing",
                    out_path,
                )
                bar_files.append(str(out_path))
                skipped_symbols.append(symbol)
                continue

            fetch_start = start
            if (
                not config.force
                and config.incremental
                and max_cached is not None
            ):
                fetch_start = max(start, max_cached)
                incremental_symbols.append(symbol)

            if fetched_count > 0 and config.symbol_delay_seconds > 0:
                sleeper(config.symbol_delay_seconds)

            logger.info("Fetching daily bars for %s (%s → %s)", symbol, fetch_start, end)
            try:
                raw_bars = massive.fetch_daily_bars(symbol, fetch_start, end)
                bars_df = bars_to_dataframe(symbol, raw_bars)
                if not config.force and config.incremental and not existing.empty:
                    bars_df = merge_bar_frames(existing, bars_df)
                out = write_parquet(bars_df, out_path)
                bar_files.append(str(out))
                fetched_count += 1
                logger.info("Wrote %d bars → %s", len(bars_df), out)
            except Exception as exc:  # noqa: BLE001 — resume-friendly per-symbol
                logger.error("Failed to fetch bars for %s: %s", symbol, exc)
                failed_symbols.append({"symbol": symbol, "error": str(exc)})

        grouped_filled = _backfill_missing_session(
            config,
            massive,
            session=last_weekday_on_or_before(end),
        )

        logger.info("Fetching splits for %d symbols", len(config.symbols))
        splits_df = splits_to_dataframe(
            massive.fetch_splits(config.symbols, start, end)
        )
        write_parquet(splits_df, paths["splits"])

        logger.info("Fetching dividends for %d symbols", len(config.symbols))
        dividends_df = dividends_to_dataframe(
            massive.fetch_dividends(config.symbols, start, end)
        )
        write_parquet(dividends_df, paths["dividends"])

        logger.info("Fetching market holidays calendar")
        calendar_df = calendar_to_dataframe(massive.fetch_market_holidays())
        write_parquet(calendar_df, paths["calendar"])
    finally:
        if owns_client:
            massive.close()

    cross_check_paths: dict[str, Path] = {}
    report = pd.DataFrame()
    if not config.skip_yfinance:
        fetcher = yfinance_fetcher or fetch_yfinance_splits
        logger.info("Cross-checking splits with yfinance")
        yf_splits = fetcher(config.symbols)
        report = cross_check_splits(splits_df, yf_splits)
        cross_check_paths = write_cross_check_report(report, paths)
        mismatch_count = (
            int((report["status"] != "match").sum()) if not report.empty else 0
        )
        logger.info(
            "Cross-check complete: %d rows, %d mismatches → %s",
            len(report),
            mismatch_count,
            cross_check_paths.get("json"),
        )

    uploaded: list[str] = []
    if config.s3_bucket:
        logger.info(
            "Uploading cache to s3://%s/%s", config.s3_bucket, config.s3_prefix
        )
        uploaded = upload_cache_to_s3(
            config.cache_dir,
            config.s3_bucket,
            config.s3_prefix,
            region=config.aws_region,
            s3_client=s3_client,
        )

    return {
        "cache_dir": str(config.cache_dir),
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "symbols": list(config.symbols),
        "bar_files": bar_files,
        "bars_fetched": fetched_count,
        "bars_skipped": skipped_symbols,
        "bars_incremental": incremental_symbols,
        "bars_failed": failed_symbols,
        "bars_grouped_backfill": grouped_filled,
        "splits_rows": int(len(splits_df)),
        "dividends_rows": int(len(dividends_df)),
        "calendar_rows": int(len(calendar_df)),
        "cross_check": {k: str(v) for k, v in cross_check_paths.items()},
        "cross_check_mismatches": (
            int((report["status"] != "match").sum()) if not report.empty else 0
        ),
        "s3_uploaded": uploaded,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Massive EOD bars / splits / dividends / calendar → Parquet cache; "
            "spot-check splits with yfinance."
        )
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="Tickers to fetch (default: provisional ~40 liquid US equities)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Local Parquet cache root (default: backend/data/cache)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="History window in days (default: 730; daily blotter uses 5)",
    )
    parser.add_argument(
        "--skip-yfinance",
        action="store_true",
        help="Skip yfinance split cross-check",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch and overwrite all bar parquets (default: incremental merge)",
    )
    parser.add_argument(
        "--skip-cached",
        action="store_true",
        help="Skip any symbol that already has a non-empty bar file (no incremental)",
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
    # httpx INFO logs full request URLs; keep keys out of the console.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    symbols = tuple(s.upper() for s in args.symbols) if args.symbols else None
    try:
        config = load_config(
            symbols=symbols,
            cache_dir=args.cache_dir,
            lookback_days=args.lookback_days,
            skip_yfinance=args.skip_yfinance,
            force=args.force,
            skip_cached=args.skip_cached,
            incremental=not args.skip_cached,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    summary = run_fetch(config)
    print(json.dumps(summary, indent=2))
    if summary.get("bars_failed"):
        logger.error(
            "Completed with %d symbol failure(s); re-run to resume",
            len(summary["bars_failed"]),
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
