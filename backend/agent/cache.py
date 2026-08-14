"""Read cached market-data Parquet (local, optional S3 fallback).

Never calls Massive / yfinance. The fetch job already wrote this cache.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

from backend.data.fetch_market_data import (
    CALENDAR_COLUMNS,
    DIVIDENDS_COLUMNS,
    SPLITS_COLUMNS,
    default_cache_dir,
    read_parquet,
)

logger = logging.getLogger(__name__)

SPLITS_FILE = "splits.parquet"
DIVIDENDS_FILE = "dividends.parquet"
CALENDAR_FILE = "calendar.parquet"


def cache_dir_from_env() -> Path:
    return default_cache_dir()


def s3_cache_settings(
    env: dict[str, str] | None = None,
) -> tuple[str | None, str, str]:
    source = env if env is not None else os.environ
    bucket = (source.get("S3_CACHE_BUCKET") or "").strip() or None
    prefix = (source.get("S3_CACHE_PREFIX") or "market-data").strip() or "market-data"
    region = (
        (source.get("AWS_REGION") or "").strip()
        or (source.get("AWS_DEFAULT_REGION") or "").strip()
        or "us-east-1"
    )
    return bucket, prefix, region


def _download_s3_file(
    bucket: str,
    key: str,
    dest: Path,
    *,
    region: str,
    s3_client: Any | None = None,
) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    client = s3_client
    if client is None:
        import boto3  # noqa: PLC0415 — optional live AWS

        client = boto3.client("s3", region_name=region)
    try:
        client.download_file(bucket, key, str(dest))
    except Exception as exc:  # noqa: BLE001 — cache miss is non-fatal
        logger.warning("Could not download s3://%s/%s (%s)", bucket, key, type(exc).__name__)
        return False
    return dest.is_file() and dest.stat().st_size > 0


def ensure_parquet(
    filename: str,
    cache_dir: Path,
    *,
    s3_bucket: str | None = None,
    s3_prefix: str = "market-data",
    aws_region: str = "us-east-1",
    s3_client: Any | None = None,
) -> Path | None:
    """Return a local path to ``filename``, downloading from S3 if needed."""
    local = cache_dir / filename
    if local.is_file() and local.stat().st_size > 0:
        return local
    if not s3_bucket:
        return None
    key = f"{s3_prefix.rstrip('/')}/{filename}"
    if _download_s3_file(
        s3_bucket, key, local, region=aws_region, s3_client=s3_client
    ):
        return local
    return None


def _read_or_empty(path: Path | None, columns: tuple[str, ...]) -> pd.DataFrame:
    if path is None:
        return pd.DataFrame(columns=list(columns))
    try:
        df = read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read %s (%s)", path, type(exc).__name__)
        return pd.DataFrame(columns=list(columns))
    if df.empty:
        return pd.DataFrame(columns=list(columns))
    return df


def load_splits(
    cache_dir: Path,
    *,
    s3_bucket: str | None = None,
    s3_prefix: str = "market-data",
    aws_region: str = "us-east-1",
    s3_client: Any | None = None,
) -> pd.DataFrame:
    path = ensure_parquet(
        SPLITS_FILE,
        cache_dir,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        aws_region=aws_region,
        s3_client=s3_client,
    )
    df = _read_or_empty(path, SPLITS_COLUMNS)
    if df.empty:
        return df
    out = df.copy()
    if "ticker" in out.columns:
        out["ticker"] = out["ticker"].astype(str).str.upper()
    return out


def load_dividends(
    cache_dir: Path,
    *,
    s3_bucket: str | None = None,
    s3_prefix: str = "market-data",
    aws_region: str = "us-east-1",
    s3_client: Any | None = None,
) -> pd.DataFrame:
    path = ensure_parquet(
        DIVIDENDS_FILE,
        cache_dir,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        aws_region=aws_region,
        s3_client=s3_client,
    )
    df = _read_or_empty(path, DIVIDENDS_COLUMNS)
    if df.empty:
        return df
    out = df.copy()
    if "ticker" in out.columns:
        out["ticker"] = out["ticker"].astype(str).str.upper()
    return out


def load_calendar(
    cache_dir: Path,
    *,
    s3_bucket: str | None = None,
    s3_prefix: str = "market-data",
    aws_region: str = "us-east-1",
    s3_client: Any | None = None,
) -> pd.DataFrame:
    path = ensure_parquet(
        CALENDAR_FILE,
        cache_dir,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        aws_region=aws_region,
        s3_client=s3_client,
    )
    return _read_or_empty(path, CALENDAR_COLUMNS)
