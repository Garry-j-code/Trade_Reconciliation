"""Normalize broker / desk raw legs into a canonical trade schema.

Pure dataframe functions — no LLM, no network, no DB I/O.

Canonical schema
----------------
trade_id         source trade id (broker_trade_id | blotter_id)
source           ``broker`` | ``desk``
symbol           symbol | ticker
trade_date       ISO date
settlement_date  settlement_date | settle_date
side             BUY / SELL (uppercased)
quantity         quantity | qty
price            price | px
currency         currency | ccy
account          account_id | desk_code
executing_party  execution_venue | trader
pair_id          preserved for round-trip identity with the generator
raw_payload      JSON object of the original raw row (optional column)
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

SOURCE_BROKER = "broker"
SOURCE_DESK = "desk"

CANONICAL_COLUMNS: tuple[str, ...] = (
    "trade_id",
    "source",
    "symbol",
    "trade_date",
    "settlement_date",
    "side",
    "quantity",
    "price",
    "currency",
    "account",
    "executing_party",
    "pair_id",
    "raw_payload",
)

# Required input columns per raw leg (pair_id is optional but preserved when present).
BROKER_REQUIRED: tuple[str, ...] = (
    "broker_trade_id",
    "symbol",
    "trade_date",
    "settlement_date",
    "side",
    "quantity",
    "price",
    "currency",
    "account_id",
    "execution_venue",
)

DESK_REQUIRED: tuple[str, ...] = (
    "blotter_id",
    "ticker",
    "trade_date",
    "settle_date",
    "side",
    "qty",
    "px",
    "ccy",
    "desk_code",
    "trader",
)


class NormalizationError(ValueError):
    """Raised when a raw frame is missing required columns or has invalid values."""


def _missing_columns(df: pd.DataFrame, required: Sequence[str]) -> list[str]:
    return [c for c in required if c not in df.columns]


def require_columns(df: pd.DataFrame, required: Sequence[str], *, leg: str) -> None:
    """Raise ``NormalizationError`` if any required columns are absent."""
    missing = _missing_columns(df, required)
    if missing:
        raise NormalizationError(
            f"{leg} trades missing required columns: {', '.join(missing)}"
        )


def _to_date_series(series: pd.Series) -> pd.Series:
    """Parse date-like values to ``datetime.date`` (NaT → None)."""
    parsed = pd.to_datetime(series, errors="coerce")
    return parsed.dt.date


def _row_payload(row: Mapping[str, Any], columns: Iterable[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for col in columns:
        val = row[col]
        if pd.isna(val):
            payload[col] = None
        elif hasattr(val, "isoformat"):
            payload[col] = val.isoformat()
        elif isinstance(val, (str, int, float, bool)) or val is None:
            payload[col] = val
        else:
            payload[col] = str(val)
    return payload


def empty_normalized() -> pd.DataFrame:
    """Empty frame with the canonical column set."""
    return pd.DataFrame(columns=list(CANONICAL_COLUMNS))


def normalize_broker_trades(
    df: pd.DataFrame,
    *,
    include_raw_payload: bool = True,
) -> pd.DataFrame:
    """Map broker columns → canonical schema; tag ``source='broker'``."""
    if df.empty:
        out = empty_normalized()
        return out

    require_columns(df, BROKER_REQUIRED, leg="broker")
    work = df.copy()
    raw_cols = list(work.columns)

    out = pd.DataFrame(
        {
            "trade_id": work["broker_trade_id"].astype(str),
            "source": SOURCE_BROKER,
            "symbol": work["symbol"].astype(str).str.upper(),
            "trade_date": _to_date_series(work["trade_date"]),
            "settlement_date": _to_date_series(work["settlement_date"]),
            "side": work["side"].astype(str).str.upper().str.strip(),
            "quantity": pd.to_numeric(work["quantity"], errors="coerce"),
            "price": pd.to_numeric(work["price"], errors="coerce"),
            "currency": work["currency"].astype(str).str.upper().str.strip(),
            "account": work["account_id"].astype(str),
            "executing_party": work["execution_venue"].astype(str),
            "pair_id": (
                work["pair_id"].astype(str)
                if "pair_id" in work.columns
                else pd.Series([None] * len(work), dtype=object)
            ),
        }
    )

    if include_raw_payload:
        out["raw_payload"] = [
            _row_payload(row, raw_cols) for row in work.to_dict(orient="records")
        ]
    else:
        out["raw_payload"] = None

    return out[list(CANONICAL_COLUMNS)].reset_index(drop=True)


def normalize_desk_trades(
    df: pd.DataFrame,
    *,
    include_raw_payload: bool = True,
) -> pd.DataFrame:
    """Map desk columns → canonical schema; tag ``source='desk'``."""
    if df.empty:
        return empty_normalized()

    require_columns(df, DESK_REQUIRED, leg="desk")
    work = df.copy()
    raw_cols = list(work.columns)

    out = pd.DataFrame(
        {
            "trade_id": work["blotter_id"].astype(str),
            "source": SOURCE_DESK,
            "symbol": work["ticker"].astype(str).str.upper(),
            "trade_date": _to_date_series(work["trade_date"]),
            "settlement_date": _to_date_series(work["settle_date"]),
            "side": work["side"].astype(str).str.upper().str.strip(),
            "quantity": pd.to_numeric(work["qty"], errors="coerce"),
            "price": pd.to_numeric(work["px"], errors="coerce"),
            "currency": work["ccy"].astype(str).str.upper().str.strip(),
            "account": work["desk_code"].astype(str),
            "executing_party": work["trader"].astype(str),
            "pair_id": (
                work["pair_id"].astype(str)
                if "pair_id" in work.columns
                else pd.Series([None] * len(work), dtype=object)
            ),
        }
    )

    if include_raw_payload:
        out["raw_payload"] = [
            _row_payload(row, raw_cols) for row in work.to_dict(orient="records")
        ]
    else:
        out["raw_payload"] = None

    return out[list(CANONICAL_COLUMNS)].reset_index(drop=True)


def combine_normalized(
    broker: pd.DataFrame,
    desk: pd.DataFrame,
) -> pd.DataFrame:
    """Stack already-normalized broker + desk frames."""
    frames = [f for f in (broker, desk) if f is not None and not f.empty]
    if not frames:
        return empty_normalized()
    for frame in frames:
        missing = [c for c in CANONICAL_COLUMNS if c not in frame.columns]
        if missing:
            raise NormalizationError(
                f"normalized frame missing columns: {', '.join(missing)}"
            )
    combined = pd.concat(
        [f[list(CANONICAL_COLUMNS)] for f in frames],
        ignore_index=True,
    )
    return combined.reset_index(drop=True)


def normalize_both(
    broker_raw: pd.DataFrame,
    desk_raw: pd.DataFrame,
    *,
    include_raw_payload: bool = True,
) -> pd.DataFrame:
    """Normalize both legs and return a single canonical frame."""
    return combine_normalized(
        normalize_broker_trades(broker_raw, include_raw_payload=include_raw_payload),
        normalize_desk_trades(desk_raw, include_raw_payload=include_raw_payload),
    )


def raw_payload_to_json(value: Any) -> str | None:
    """Serialize a raw_payload cell for Parquet (stores as JSON string)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def prepare_normalized_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    """Copy with ``raw_payload`` as JSON strings and dates as ISO strings."""
    out = df.copy()
    if "raw_payload" in out.columns:
        out["raw_payload"] = out["raw_payload"].map(raw_payload_to_json)
    for col in ("trade_date", "settlement_date"):
        if col in out.columns:
            out[col] = out[col].map(
                lambda d: d.isoformat() if hasattr(d, "isoformat") else d
            )
    return out
