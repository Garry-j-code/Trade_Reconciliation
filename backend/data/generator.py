"""Synthetic broker + desk trade generator from cached market data.

Reads Parquet written by ``fetch_market_data`` (bars, splits, calendar).
Never calls Massive, yfinance, or any live market API.

Produces two raw legs with intentionally different column names (normalization
is step 3) plus a ground-truth manifest of injected / corporate-action breaks.

Usage:
    uv run generate-trades
    uv run python -m backend.data.generator

Env:
    MARKET_DATA_CACHE_DIR   (default: backend/data/cache)
    TRADE_OUTPUT_DIR        (default: backend/data/generated)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from backend.data.fetch_market_data import (
    bar_path,
    default_cache_dir,
    read_parquet,
)

logger = logging.getLogger(__name__)

NYSE_TZ = ZoneInfo("America/New_York")
# Regular session 09:30–16:00 ET (last fill can land in the final minute).
_SESSION_OPEN = timedelta(hours=9, minutes=30)
_SESSION_MINUTES = 6 * 60 + 30

# ---------------------------------------------------------------------------
# Output schemas (raw legs differ on purpose — see project_plan.md §4)
# ---------------------------------------------------------------------------

BROKER_COLUMNS: tuple[str, ...] = (
    "broker_trade_id",
    "symbol",
    "trade_date",
    "executed_at",
    "settlement_date",
    "settlement_datetime",
    "side",
    "quantity",
    "price",
    "currency",
    "account_id",
    "execution_venue",
    "pair_id",
)

DESK_COLUMNS: tuple[str, ...] = (
    "blotter_id",
    "ticker",
    "trade_date",
    "executed_at",
    "settle_date",
    "settlement_datetime",
    "side",
    "qty",
    "px",
    "ccy",
    "desk_code",
    "trader",
    "pair_id",
)

GROUND_TRUTH_COLUMNS: tuple[str, ...] = (
    "pair_id",
    "break_type",
    "broker_trade_ids",
    "desk_trade_ids",
    "symbol",
    "trade_date",
    "detail",
)

# Break types produced by the generator (ground-truth labels).
BREAK_CLEAN = "clean"
BREAK_MISSING_BROKER = "missing_broker"
BREAK_MISSING_DESK = "missing_desk"
BREAK_PRICE = "price_break"
BREAK_QUANTITY = "quantity_break"
BREAK_DUPLICATE = "duplicate"
BREAK_SETTLEMENT = "settlement_date_mismatch"
BREAK_SPLIT_FILL = "split_fill"
BREAK_CORPORATE_ACTION = "corporate_action"

INJECTED_BREAK_TYPES: tuple[str, ...] = (
    BREAK_MISSING_BROKER,
    BREAK_MISSING_DESK,
    BREAK_PRICE,
    BREAK_QUANTITY,
    BREAK_DUPLICATE,
    BREAK_SETTLEMENT,
    BREAK_SPLIT_FILL,
)


@dataclass(frozen=True)
class BreakRates:
    """Per-pair probabilities for injected (non-corporate-action) breaks.

    Rates are applied sequentially against remaining clean pairs; they need not
    sum to 1.0. Corporate-action breaks are created separately from real splits.
    """

    missing_broker: float = 0.04
    missing_desk: float = 0.04
    price_break: float = 0.05
    quantity_break: float = 0.03
    duplicate: float = 0.03
    settlement_date_mismatch: float = 0.03
    split_fill: float = 0.05


@dataclass
class GeneratorConfig:
    """Runtime config for a synthetic trade generation run."""

    cache_dir: Path = field(default_factory=default_cache_dir)
    output_dir: Path = field(default_factory=lambda: Path("backend/data/generated"))
    symbols: tuple[str, ...] | None = None
    n_trades: int = 500
    seed: int = 42
    trade_date: date | None = None
    rates: BreakRates = field(default_factory=BreakRates)
    # Max corporate-action lag breaks to create (0 = skip; None = one per split).
    max_corporate_action_breaks: int | None = None
    # Window around ``trade_date`` for real splits used as CA lag breaks.
    corporate_action_window_days: int = 14
    desks: tuple[str, ...] = ("EQ-US", "EQ-ARB", "EQ-INDEX")
    traders: tuple[str, ...] = ("T.NGUYEN", "A.PATEL", "J.KIM", "S.MORALES")
    broker_account: str = "CLR-001"
    currency: str = "USD"
    price_break_bps: float = 75.0  # desk price skewed by this many bps
    quantity_break_pct: float = 0.10  # desk qty off by this fraction


@dataclass
class MarketCache:
    """In-memory view of the Parquet market-data cache."""

    bars: pd.DataFrame
    splits: pd.DataFrame
    calendar: pd.DataFrame
    cache_dir: Path


@dataclass
class GenerateResult:
    broker: pd.DataFrame
    desk: pd.DataFrame
    ground_truth: pd.DataFrame
    summary: dict[str, Any]


def default_output_dir() -> Path:
    override = os.environ.get("TRADE_OUTPUT_DIR", "").strip()
    if override:
        return Path(override)
    return Path("backend/data/generated")


def load_generator_config(
    *,
    cache_dir: Path | None = None,
    output_dir: Path | None = None,
    symbols: tuple[str, ...] | None = None,
    n_trades: int | None = None,
    seed: int | None = None,
    trade_date: date | None = None,
    env: Mapping[str, str] | None = None,
) -> GeneratorConfig:
    """Build GeneratorConfig from env + optional CLI overrides."""
    _ = env  # reserved for future env knobs; cache path shared with fetch
    dated = trade_date is not None
    default_n = 40 if dated else 500
    return GeneratorConfig(
        cache_dir=cache_dir or default_cache_dir(),
        output_dir=output_dir or default_output_dir(),
        symbols=symbols,
        n_trades=n_trades if n_trades is not None else default_n,
        seed=seed if seed is not None else 42,
        trade_date=trade_date,
    )


def seed_for_trade_date(base_seed: int, trade_date: date) -> int:
    """Stable 32-bit RNG seed: ``base_seed`` namespaced by ISO date.

    Re-running ``2026-08-13`` with ``seed=42`` always yields the same trades.
    """
    payload = f"{int(base_seed)}:{trade_date.isoformat()}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()[:8]
    return int.from_bytes(digest, "big") % (2**32)


def parse_iso_date(value: str) -> date:
    """Parse ``YYYY-MM-DD`` into a ``date``."""
    return date.fromisoformat(str(value).strip())


def last_completed_us_session(
    as_of: date,
    closed: set[date],
) -> date:
    """Most recent US equity session on or before ``as_of`` (skips weekends/holidays)."""
    current = as_of
    for _ in range(366):
        if current.weekday() < 5 and current not in closed:
            return current
        current = current - timedelta(days=1)
    raise ValueError(f"No US equity session found on or before {as_of.isoformat()}")


def last_cached_us_session(
    bars: pd.DataFrame,
    as_of: date,
    closed: set[date],
) -> date:
    """Newest session that actually has bars, at or before ``as_of``.

    The market-data provider publishes T-1 on this plan: grouped-daily is
    denied and per-ticker ``/prev`` returns the prior session, so the session
    that just closed is still missing when the EOD job fetches. Anchoring on
    the newest cached session reconciles that one instead of failing on a
    session with no bars.
    """
    limit = last_completed_us_session(as_of, closed)
    if bars.empty or "date" not in bars.columns:
        raise ValueError("Market-data cache has no bars; run `uv run fetch-market-data`")
    sessions = {parse_iso_date(str(value)[:10]) for value in bars["date"].dropna().unique()}
    eligible = [d for d in sessions if d <= limit and d.weekday() < 5 and d not in closed]
    if not eligible:
        raise ValueError(
            f"Market-data cache has no session on or before {limit.isoformat()}"
        )
    return max(eligible)


def prior_us_sessions(
    n: int,
    *,
    as_of: date,
    closed: set[date],
) -> list[date]:
    """Last ``n`` completed US sessions ending at ``last_completed_us_session(as_of)``."""
    if n < 1:
        raise ValueError("n must be >= 1")
    last = last_completed_us_session(as_of, closed)
    days: list[date] = []
    cursor = last
    while len(days) < n:
        if cursor.weekday() < 5 and cursor not in closed:
            days.append(cursor)
        cursor = cursor - timedelta(days=1)
        if (last - cursor).days > 400:
            break
    days.reverse()
    return days


def _empty_broker() -> pd.DataFrame:
    return pd.DataFrame(columns=list(BROKER_COLUMNS))


def _empty_desk() -> pd.DataFrame:
    return pd.DataFrame(columns=list(DESK_COLUMNS))


def _empty_ground_truth() -> pd.DataFrame:
    return pd.DataFrame(columns=list(GROUND_TRUTH_COLUMNS))


def load_market_cache(
    cache_dir: Path,
    *,
    symbols: Sequence[str] | None = None,
) -> MarketCache:
    """Load bars / splits / calendar from a fetch_market_data cache tree.

    Raises FileNotFoundError if the cache layout is missing or empty.
    """
    bars_dir = cache_dir / "bars"
    splits_path = cache_dir / "splits.parquet"
    calendar_path = cache_dir / "calendar.parquet"

    if not bars_dir.is_dir():
        raise FileNotFoundError(
            f"Bars directory not found: {bars_dir}. "
            "Run `uv run fetch-market-data` first."
        )

    wanted = (
        tuple(s.upper() for s in symbols)
        if symbols
        else tuple(
            p.stem.upper()
            for p in sorted(bars_dir.glob("*.parquet"))
            if p.stem
        )
    )
    if not wanted:
        raise FileNotFoundError(f"No bar Parquet files under {bars_dir}")

    frames: list[pd.DataFrame] = []
    missing: list[str] = []
    for symbol in wanted:
        path = bar_path(cache_dir, symbol)
        if not path.is_file():
            missing.append(symbol)
            continue
        df = read_parquet(path)
        if df.empty:
            continue
        frames.append(df)

    if not frames:
        raise FileNotFoundError(
            f"No usable bar data for symbols={wanted}; missing files={missing}"
        )
    if missing:
        logger.warning("Skipping symbols with no bar file: %s", ", ".join(missing))

    bars = pd.concat(frames, ignore_index=True)
    bars["ticker"] = bars["ticker"].astype(str).str.upper()
    bars["date"] = pd.to_datetime(bars["date"]).dt.date

    if splits_path.is_file():
        splits = read_parquet(splits_path)
    else:
        logger.warning("splits.parquet missing at %s; using empty splits", splits_path)
        splits = pd.DataFrame(
            columns=[
                "ticker",
                "execution_date",
                "split_from",
                "split_to",
                "adjustment_type",
                "historical_adjustment_factor",
                "id",
            ]
        )
    if not splits.empty:
        splits = splits.copy()
        splits["ticker"] = splits["ticker"].astype(str).str.upper()
        splits["execution_date"] = pd.to_datetime(splits["execution_date"]).dt.date

    if calendar_path.is_file():
        calendar = read_parquet(calendar_path)
    else:
        logger.warning(
            "calendar.parquet missing at %s; weekends-only settlement calendar",
            calendar_path,
        )
        calendar = pd.DataFrame(
            columns=["date", "exchange", "name", "status", "open", "close"]
        )
    if not calendar.empty:
        calendar = calendar.copy()
        calendar["date"] = pd.to_datetime(calendar["date"]).dt.date

    return MarketCache(
        bars=bars, splits=splits, calendar=calendar, cache_dir=cache_dir
    )


def closed_market_dates(calendar: pd.DataFrame) -> set[date]:
    """Return dates the market is fully closed (not early-close)."""
    if calendar.empty or "status" not in calendar.columns:
        return set()
    closed = calendar[calendar["status"].astype(str).str.lower() == "closed"]
    dates = pd.to_datetime(closed["date"]).dt.date
    return set(dates.tolist())


def next_business_day(
    start: date,
    *,
    closed: set[date],
    offset: int = 1,
) -> date:
    """Advance ``offset`` weekdays, skipping weekends and closed calendar days."""
    if offset < 1:
        raise ValueError("offset must be >= 1")
    current = start
    remaining = offset
    while remaining > 0:
        current = current + timedelta(days=1)
        if current.weekday() >= 5:
            continue
        if current in closed:
            continue
        remaining -= 1
    return current


def settlement_date_for(
    trade_date: date,
    *,
    closed: set[date],
    cycle: int = 1,
) -> date:
    """US equity settlement (T+1 as of May 2024)."""
    return next_business_day(trade_date, closed=closed, offset=cycle)


def split_ratio(split_from: Any, split_to: Any) -> float | None:
    """Return split_to / split_from, or None if invalid."""
    try:
        frm = float(split_from)
        to = float(split_to)
    except (TypeError, ValueError):
        return None
    if frm == 0:
        return None
    return to / frm


def _new_id(prefix: str, rng: Any) -> str:
    """Deterministic-ish id from the run RNG (seeded runs reproduce)."""
    return f"{prefix}-{rng.bytes(6).hex()}"


def _pick_side(rng: Any) -> str:
    return "BUY" if rng.random() < 0.5 else "SELL"


def _round_lot_qty(rng: Any) -> int:
    """100–5_000 shares in round lots of 100."""
    return int(rng.integers(1, 51)) * 100


def _price_from_bar(row: Mapping[str, Any], rng: Any) -> float:
    """Sample an execution price inside the day's OHLC range (prefer close/vwap)."""
    low = float(row["low"])
    high = float(row["high"])
    close = float(row["close"])
    vwap = row.get("vwap")
    anchor = float(vwap) if vwap is not None and not pd.isna(vwap) else close
    # Small noise around VWAP/close, clipped to [low, high].
    noise = float(rng.normal(0.0, max((high - low) * 0.05, 0.01)))
    px = anchor + noise
    px = min(max(px, low), high)
    return round(px, 4)


def session_executed_at(
    trade_date: date, rng: Any, *, fill_index: int = 0
) -> datetime:
    """Deterministic NYSE-hours timestamp on ``trade_date`` (America/New_York)."""
    minute_of_session = int(rng.integers(0, _SESSION_MINUTES))
    second = int(rng.integers(0, 60))
    start = datetime(
        trade_date.year, trade_date.month, trade_date.day, tzinfo=NYSE_TZ
    ) + _SESSION_OPEN
    return start + timedelta(
        minutes=minute_of_session, seconds=second + fill_index * 3
    )


def _session_executed_at(
    trade_date: date, rng: Any, *, fill_index: int = 0
) -> datetime:
    return session_executed_at(trade_date, rng, fill_index=fill_index)


def settlement_datetime_et(settle: date) -> datetime:
    """End of the NYSE cash session on the settlement date (not midnight)."""
    return datetime(settle.year, settle.month, settle.day, 16, 0, 0, tzinfo=NYSE_TZ)


def _settlement_datetime(settle: date) -> datetime:
    return settlement_datetime_et(settle)


def executed_at_from_stable_id(stable_id: str, trade_date: date) -> datetime:
    """NYSE-session time on ``trade_date``, seeded from ``stable_id`` (idempotent)."""
    digest = hashlib.sha256(str(stable_id).encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big") % (2**32)
    rng = np.random.default_rng(seed)
    return session_executed_at(trade_date, rng)


def _broker_row(
    *,
    trade_id: str,
    symbol: str,
    trade_date: date,
    settle: date,
    side: str,
    quantity: float,
    price: float,
    pair_id: str,
    account_id: str,
    currency: str,
    executed_at: datetime,
) -> dict[str, Any]:
    return {
        "broker_trade_id": trade_id,
        "symbol": symbol,
        "trade_date": trade_date.isoformat(),
        "executed_at": executed_at.isoformat(),
        "settlement_date": settle.isoformat(),
        "settlement_datetime": _settlement_datetime(settle).isoformat(),
        "side": side,
        "quantity": float(quantity),
        "price": float(price),
        "currency": currency,
        "account_id": account_id,
        "execution_venue": "XNYS",
        "pair_id": pair_id,
    }


def _desk_row(
    *,
    trade_id: str,
    symbol: str,
    trade_date: date,
    settle: date,
    side: str,
    quantity: float,
    price: float,
    pair_id: str,
    desk_code: str,
    trader: str,
    currency: str,
    executed_at: datetime,
) -> dict[str, Any]:
    return {
        "blotter_id": trade_id,
        "ticker": symbol,
        "trade_date": trade_date.isoformat(),
        "executed_at": executed_at.isoformat(),
        "settle_date": settle.isoformat(),
        "settlement_datetime": _settlement_datetime(settle).isoformat(),
        "side": side,
        "qty": float(quantity),
        "px": float(price),
        "ccy": currency,
        "desk_code": desk_code,
        "trader": trader,
        "pair_id": pair_id,
    }


def _truth_row(
    *,
    pair_id: str,
    break_type: str,
    broker_ids: Sequence[str],
    desk_ids: Sequence[str],
    symbol: str,
    trade_date: date,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "pair_id": pair_id,
        "break_type": break_type,
        "broker_trade_ids": ",".join(broker_ids),
        "desk_trade_ids": ",".join(desk_ids),
        "symbol": symbol,
        "trade_date": trade_date.isoformat(),
        "detail": detail,
    }


def _eligible_bar_pool(bars: pd.DataFrame) -> pd.DataFrame:
    """Drop rows we cannot price from."""
    required = ["ticker", "date", "open", "high", "low", "close"]
    pool = bars.dropna(subset=required).copy()
    # Need a valid range
    pool = pool[pool["high"] >= pool["low"]]
    return pool.reset_index(drop=True)


def _sample_base_trades(
    pool: pd.DataFrame,
    *,
    n: int,
    rng: Any,
    closed: set[date],
    config: GeneratorConfig,
) -> list[dict[str, Any]]:
    """Sample n clean-intent trade skeletons from the bar pool."""
    if pool.empty:
        raise ValueError("Bar pool is empty — cannot sample trades")
    n_pool = len(pool)
    idxs = rng.integers(0, n_pool, size=n)
    bases: list[dict[str, Any]] = []
    for i in range(n):
        row = pool.iloc[int(idxs[i])]
        trade_date = row["date"]
        if isinstance(trade_date, datetime):
            trade_date = trade_date.date()
        settle = settlement_date_for(trade_date, closed=closed)
        qty = _round_lot_qty(rng)
        price = _price_from_bar(row, rng)
        side = _pick_side(rng)
        bases.append(
            {
                "symbol": str(row["ticker"]).upper(),
                "trade_date": trade_date,
                "settlement_date": settle,
                "side": side,
                "quantity": float(qty),
                "price": float(price),
                "desk_code": str(rng.choice(config.desks)),
                "trader": str(rng.choice(config.traders)),
            }
        )
    return bases


def _assign_break_types(
    n: int,
    rates: BreakRates,
    rng: Any,
) -> list[str]:
    """Assign an injected break type (or clean) to each of n pairs."""
    labels = [BREAK_CLEAN] * n
    remaining = list(range(n))
    rng.shuffle(remaining)

    plan: list[tuple[str, float]] = [
        (BREAK_MISSING_BROKER, rates.missing_broker),
        (BREAK_MISSING_DESK, rates.missing_desk),
        (BREAK_PRICE, rates.price_break),
        (BREAK_QUANTITY, rates.quantity_break),
        (BREAK_DUPLICATE, rates.duplicate),
        (BREAK_SETTLEMENT, rates.settlement_date_mismatch),
        (BREAK_SPLIT_FILL, rates.split_fill),
    ]

    for break_type, rate in plan:
        if rate <= 0 or not remaining:
            continue
        k = min(len(remaining), max(0, int(round(n * rate))))
        chosen = remaining[:k]
        remaining = remaining[k:]
        for idx in chosen:
            labels[idx] = break_type
    return labels


def _split_quantity_into_fills(total: float, n_fills: int, rng: Any) -> list[float]:
    """Split total quantity into n positive fill sizes that sum to total."""
    if n_fills < 2:
        raise ValueError("n_fills must be >= 2")
    # Work in whole shares when total is integral.
    whole = abs(total - round(total)) < 1e-9
    units = int(round(total)) if whole else None
    if units is not None and units >= n_fills:
        cuts = rng.choice(units - 1, size=n_fills - 1, replace=False) + 1
        cut_list = sorted(int(x) for x in np.asarray(cuts).tolist())
        edges = [0, *cut_list, units]
        parts = [float(edges[i + 1] - edges[i]) for i in range(n_fills)]
        return parts
    weights = rng.random(n_fills)
    weights = weights / weights.sum()
    parts = [round(float(total * w), 4) for w in weights]
    # Fix rounding drift on the last fill.
    parts[-1] = round(float(total) - sum(parts[:-1]), 4)
    return parts


def _materialize_pair(
    base: Mapping[str, Any],
    break_type: str,
    *,
    rng: Any,
    closed: set[date],
    config: GeneratorConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Turn one skeleton + break label into broker/desk rows + ground truth."""
    pair_id = _new_id("PAIR", rng)
    symbol = str(base["symbol"])
    trade_date: date = base["trade_date"]  # type: ignore[assignment]
    settle: date = base["settlement_date"]  # type: ignore[assignment]
    side = str(base["side"])
    qty = float(base["quantity"])
    price = float(base["price"])
    desk_code = str(base["desk_code"])
    trader = str(base["trader"])
    executed_at = _session_executed_at(trade_date, rng)
    fill_index = 0

    broker_rows: list[dict[str, Any]] = []
    desk_rows: list[dict[str, Any]] = []

    def add_broker(
        quantity: float,
        px: float,
        settle_date: date = settle,
        trade_id: str | None = None,
    ) -> str:
        nonlocal fill_index
        tid = trade_id or _new_id("BRK", rng)
        ts = executed_at + timedelta(seconds=fill_index * 3)
        fill_index += 1
        broker_rows.append(
            _broker_row(
                trade_id=tid,
                symbol=symbol,
                trade_date=trade_date,
                settle=settle_date,
                side=side,
                quantity=quantity,
                price=px,
                pair_id=pair_id,
                account_id=config.broker_account,
                currency=config.currency,
                executed_at=ts,
            )
        )
        return tid

    def add_desk(
        quantity: float,
        px: float,
        settle_date: date = settle,
        trade_id: str | None = None,
    ) -> str:
        tid = trade_id or _new_id("DSK", rng)
        desk_rows.append(
            _desk_row(
                trade_id=tid,
                symbol=symbol,
                trade_date=trade_date,
                settle=settle_date,
                side=side,
                quantity=quantity,
                price=px,
                pair_id=pair_id,
                desk_code=desk_code,
                trader=trader,
                currency=config.currency,
                executed_at=executed_at,
            )
        )
        return tid

    detail = ""

    if break_type == BREAK_CLEAN:
        b_id = add_broker(qty, price)
        d_id = add_desk(qty, price)
        broker_ids, desk_ids = [b_id], [d_id]

    elif break_type == BREAK_MISSING_BROKER:
        d_id = add_desk(qty, price)
        broker_ids, desk_ids = [], [d_id]
        detail = "Desk-only trade (missing on broker)"

    elif break_type == BREAK_MISSING_DESK:
        b_id = add_broker(qty, price)
        broker_ids, desk_ids = [b_id], []
        detail = "Broker-only trade (missing on desk)"

    elif break_type == BREAK_PRICE:
        skew = 1.0 + (config.price_break_bps / 10_000.0)
        desk_px = round(price * skew, 4)
        b_id = add_broker(qty, price)
        d_id = add_desk(qty, desk_px)
        broker_ids, desk_ids = [b_id], [d_id]
        detail = f"Desk price skewed by {config.price_break_bps} bps"

    elif break_type == BREAK_QUANTITY:
        desk_qty = max(1.0, round(qty * (1.0 + config.quantity_break_pct)))
        # Keep round lots when possible
        if desk_qty % 100 != 0:
            desk_qty = float(max(100, int(round(desk_qty / 100.0)) * 100))
        if desk_qty == qty:
            desk_qty = qty + 100.0
        b_id = add_broker(qty, price)
        d_id = add_desk(desk_qty, price)
        broker_ids, desk_ids = [b_id], [d_id]
        detail = f"Non-CA quantity mismatch ({qty} vs {desk_qty})"

    elif break_type == BREAK_DUPLICATE:
        b_id = add_broker(qty, price)
        # Accidental second broker booking of the same economic trade
        dup_id = add_broker(qty, price)
        d_id = add_desk(qty, price)
        broker_ids, desk_ids = [b_id, dup_id], [d_id]
        detail = "Duplicate broker booking"

    elif break_type == BREAK_SETTLEMENT:
        wrong_settle = next_business_day(settle, closed=closed, offset=1)
        b_id = add_broker(qty, price, settle_date=settle)
        d_id = add_desk(qty, price, settle_date=wrong_settle)
        broker_ids, desk_ids = [b_id], [d_id]
        detail = f"Desk settle {wrong_settle.isoformat()} vs broker {settle.isoformat()}"

    elif break_type == BREAK_SPLIT_FILL:
        n_fills = int(rng.integers(2, 5))  # 2–4 fills
        fill_qtys = _split_quantity_into_fills(qty, n_fills, rng)
        broker_ids = [add_broker(fq, price) for fq in fill_qtys]
        d_id = add_desk(qty, price)
        desk_ids = [d_id]
        detail = f"One desk block vs {n_fills} broker fills"

    else:
        raise ValueError(f"Unknown injected break_type: {break_type}")

    truth = _truth_row(
        pair_id=pair_id,
        break_type=break_type,
        broker_ids=broker_ids,
        desk_ids=desk_ids,
        symbol=symbol,
        trade_date=trade_date,
        detail=detail,
    )
    return broker_rows, desk_rows, truth


def _bar_on_or_before(
    bars: pd.DataFrame,
    symbol: str,
    as_of: date,
) -> Mapping[str, Any] | None:
    subset = bars[(bars["ticker"] == symbol) & (bars["date"] <= as_of)]
    if subset.empty:
        return None
    return subset.sort_values("date").iloc[-1].to_dict()


def generate_corporate_action_breaks(
    cache: MarketCache,
    *,
    rng: Any,
    closed: set[date],
    config: GeneratorConfig,
    max_breaks: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Create quantity mismatches from real cached split factors.

    Model: broker has already applied the split (post-split qty / price);
    desk still books pre-split qty / price (processing lag).
    """
    broker_rows: list[dict[str, Any]] = []
    desk_rows: list[dict[str, Any]] = []
    truth_rows: list[dict[str, Any]] = []

    if cache.splits.empty:
        return broker_rows, desk_rows, truth_rows

    usable: list[dict[str, Any]] = []
    for raw in cache.splits.to_dict(orient="records"):
        ratio = split_ratio(raw.get("split_from"), raw.get("split_to"))
        if ratio is None or ratio == 1.0:
            continue
        symbol = str(raw["ticker"]).upper()
        exec_date = raw["execution_date"]
        if isinstance(exec_date, datetime):
            exec_date = exec_date.date()
        if config.trade_date is not None:
            delta = abs((exec_date - config.trade_date).days)
            if delta > int(config.corporate_action_window_days):
                continue
        # Prefer a bar on/before the day before execution (pre-split tape).
        pre_date = exec_date - timedelta(days=1)
        if config.trade_date is not None:
            bar = _bar_on_or_before(cache.bars, symbol, config.trade_date)
        else:
            bar = _bar_on_or_before(cache.bars, symbol, pre_date)
        if bar is None:
            # Fall back to any bar for the symbol
            sym_bars = cache.bars[cache.bars["ticker"] == symbol]
            if sym_bars.empty:
                continue
            bar = sym_bars.sort_values("date").iloc[-1].to_dict()
        usable.append(
            {
                "symbol": symbol,
                "execution_date": exec_date,
                "ratio": ratio,
                "split_from": raw.get("split_from"),
                "split_to": raw.get("split_to"),
                "bar": bar,
            }
        )

    if not usable:
        return broker_rows, desk_rows, truth_rows

    limit = len(usable) if max_breaks is None else max(0, max_breaks)
    usable = usable[:limit]

    for item in usable:
        pair_id = _new_id("PAIR", rng)
        symbol = item["symbol"]
        ratio = float(item["ratio"])
        bar = item["bar"]
        trade_date = bar["date"]
        if isinstance(trade_date, datetime):
            trade_date = trade_date.date()
        if config.trade_date is not None:
            trade_date = config.trade_date
        settle = settlement_date_for(trade_date, closed=closed)
        side = _pick_side(rng)
        # Pre-split (desk-lag) quantity / price
        desk_qty = float(_round_lot_qty(rng))
        desk_px = float(_price_from_bar(bar, rng))
        # Broker has adjusted: shares * ratio, price / ratio
        broker_qty = round(desk_qty * ratio, 4)
        broker_px = round(desk_px / ratio, 4)

        b_id = _new_id("BRK", rng)
        d_id = _new_id("DSK", rng)
        executed_at = _session_executed_at(trade_date, rng)
        broker_rows.append(
            _broker_row(
                trade_id=b_id,
                symbol=symbol,
                trade_date=trade_date,
                settle=settle,
                side=side,
                quantity=broker_qty,
                price=broker_px,
                pair_id=pair_id,
                account_id=config.broker_account,
                currency=config.currency,
                executed_at=executed_at,
            )
        )
        desk_rows.append(
            _desk_row(
                trade_id=d_id,
                symbol=symbol,
                trade_date=trade_date,
                settle=settle,
                side=side,
                quantity=desk_qty,
                price=desk_px,
                pair_id=pair_id,
                desk_code=str(rng.choice(config.desks)),
                trader=str(rng.choice(config.traders)),
                currency=config.currency,
                executed_at=executed_at,
            )
        )
        truth_rows.append(
            _truth_row(
                pair_id=pair_id,
                break_type=BREAK_CORPORATE_ACTION,
                broker_ids=[b_id],
                desk_ids=[d_id],
                symbol=symbol,
                trade_date=trade_date,
                detail=(
                    f"Split {item['split_from']}:{item['split_to']} "
                    f"(ratio={ratio}) on {item['execution_date'].isoformat()}; "
                    f"broker adjusted, desk lag"
                ),
            )
        )

    return broker_rows, desk_rows, truth_rows


def generate_trades(
    cache: MarketCache,
    config: GeneratorConfig | None = None,
) -> GenerateResult:
    """Generate broker + desk legs and a ground-truth break manifest."""
    cfg = config or GeneratorConfig(cache_dir=cache.cache_dir)
    rng_seed = (
        seed_for_trade_date(cfg.seed, cfg.trade_date)
        if cfg.trade_date is not None
        else cfg.seed
    )
    rng = np.random.default_rng(rng_seed)

    pool = _eligible_bar_pool(cache.bars)
    if cfg.symbols:
        wanted = {s.upper() for s in cfg.symbols}
        pool = pool[pool["ticker"].isin(wanted)].reset_index(drop=True)
    if cfg.trade_date is not None:
        pool = pool[pool["date"] == cfg.trade_date].reset_index(drop=True)
    if pool.empty:
        if cfg.trade_date is not None:
            raise ValueError(
                f"No eligible bars for trade_date={cfg.trade_date.isoformat()} "
                "(weekend/holiday or cache gap)"
            )
        raise ValueError("No eligible bars after symbol filter")

    closed = closed_market_dates(cache.calendar)
    bases = _sample_base_trades(
        pool, n=cfg.n_trades, rng=rng, closed=closed, config=cfg
    )
    labels = _assign_break_types(cfg.n_trades, cfg.rates, rng)

    broker_acc: list[dict[str, Any]] = []
    desk_acc: list[dict[str, Any]] = []
    truth_acc: list[dict[str, Any]] = []

    for base, label in zip(bases, labels, strict=True):
        b_rows, d_rows, truth = _materialize_pair(
            base, label, rng=rng, closed=closed, config=cfg
        )
        broker_acc.extend(b_rows)
        desk_acc.extend(d_rows)
        truth_acc.append(truth)

    ca_broker, ca_desk, ca_truth = generate_corporate_action_breaks(
        cache,
        rng=rng,
        closed=closed,
        config=cfg,
        max_breaks=cfg.max_corporate_action_breaks,
    )
    broker_acc.extend(ca_broker)
    desk_acc.extend(ca_desk)
    truth_acc.extend(ca_truth)

    broker_df = (
        pd.DataFrame(broker_acc, columns=list(BROKER_COLUMNS))
        if broker_acc
        else _empty_broker()
    )
    desk_df = (
        pd.DataFrame(desk_acc, columns=list(DESK_COLUMNS))
        if desk_acc
        else _empty_desk()
    )
    truth_df = (
        pd.DataFrame(truth_acc, columns=list(GROUND_TRUTH_COLUMNS))
        if truth_acc
        else _empty_ground_truth()
    )

    counts = (
        truth_df["break_type"].value_counts().to_dict() if not truth_df.empty else {}
    )
    summary: dict[str, Any] = {
        "n_pairs": int(len(truth_df)),
        "n_broker_rows": int(len(broker_df)),
        "n_desk_rows": int(len(desk_df)),
        "break_counts": {str(k): int(v) for k, v in counts.items()},
        "seed": cfg.seed,
        "rng_seed": int(rng_seed),
        "trade_date": cfg.trade_date.isoformat() if cfg.trade_date else None,
        "n_trades_requested": cfg.n_trades,
        "cache_dir": str(cache.cache_dir),
        "symbols_in_bars": sorted(pool["ticker"].unique().tolist()),
        "corporate_action_splits_used": int(counts.get(BREAK_CORPORATE_ACTION, 0)),
    }
    return GenerateResult(
        broker=broker_df, desk=desk_df, ground_truth=truth_df, summary=summary
    )


def write_generated_trades(
    result: GenerateResult,
    output_dir: Path,
) -> dict[str, Path]:
    """Write broker / desk / ground_truth Parquet (+ summary JSON)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "broker": output_dir / "broker_trades.parquet",
        "desk": output_dir / "desk_trades.parquet",
        "ground_truth": output_dir / "ground_truth.parquet",
        "summary": output_dir / "generation_summary.json",
    }
    result.broker.to_parquet(paths["broker"], index=False)
    result.desk.to_parquet(paths["desk"], index=False)
    result.ground_truth.to_parquet(paths["ground_truth"], index=False)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **result.summary,
        "paths": {k: str(v) for k, v in paths.items() if k != "summary"},
    }
    paths["summary"].write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return paths


HOSTED_APP_ROOT = Path("/opt/trade-recon/app")


def should_delete_generated_after_db(*, env: Mapping[str, str] | None = None) -> bool:
    """True on the API EC2 (or when TRADE_RECON_DELETE_GENERATED is set).

    Hosted rematch reads RDS. The next blotter run rewrites these files.
    """
    source = env if env is not None else os.environ
    flag = (source.get("TRADE_RECON_DELETE_GENERATED") or "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    if flag in {"0", "false", "no"}:
        return False
    return HOSTED_APP_ROOT.is_dir()


def delete_generated_trade_files(output_dir: Path) -> list[str]:
    """Remove blotter Parquet (and generation_summary.json) under ``output_dir``."""
    removed: list[str] = []
    if not output_dir.is_dir():
        return removed
    for path in sorted(output_dir.glob("*.parquet")):
        path.unlink()
        removed.append(str(path))
    summary = output_dir / "generation_summary.json"
    if summary.is_file():
        summary.unlink()
        removed.append(str(summary))
    return removed


def run_generate(config: GeneratorConfig) -> dict[str, Any]:
    """Load cache → generate → write artifacts; return summary dict."""
    cache = load_market_cache(config.cache_dir, symbols=config.symbols)
    result = generate_trades(cache, config)
    paths = write_generated_trades(result, config.output_dir)
    out = dict(result.summary)
    out["output_dir"] = str(config.output_dir)
    out["paths"] = {k: str(v) for k, v in paths.items()}
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate synthetic broker + desk trades from cached market-data Parquet; "
            "inject non-CA breaks at controlled rates; CA breaks use real split factors."
        )
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Market-data cache root (default: backend/data/cache)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to write trade Parquet (default: backend/data/generated)",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="Limit to these tickers (default: all symbols present in cache)",
    )
    parser.add_argument(
        "--n-trades",
        type=int,
        default=None,
        help="Synthetic pairs before CA extras (default: 40 dated / 500 --all-history)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Base RNG seed; dated runs namespace this by trade_date (default: 42)",
    )
    parser.add_argument(
        "--trade-date",
        type=str,
        default=None,
        help="Generate only this US session (YYYY-MM-DD). Default: last completed session",
    )
    parser.add_argument(
        "--all-history",
        action="store_true",
        help="Sample trade dates across the full bar cache (legacy random blotter)",
    )
    parser.add_argument(
        "--max-corporate-action-breaks",
        type=int,
        default=None,
        help="Cap CA lag breaks (default: one per usable cached split)",
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

    symbols = tuple(s.upper() for s in args.symbols) if args.symbols else None
    trade_date: date | None = None
    try:
        if not args.all_history:
            cache_preview = load_market_cache(
                args.cache_dir or default_cache_dir(), symbols=symbols
            )
            closed = closed_market_dates(cache_preview.calendar)
            if args.trade_date:
                trade_date = parse_iso_date(args.trade_date)
            else:
                trade_date = last_completed_us_session(date.today(), closed)
            if trade_date.weekday() >= 5 or trade_date in closed:
                logger.error(
                    "%s is not a US equity session (weekend or holiday)",
                    trade_date.isoformat(),
                )
                return 1
        config = load_generator_config(
            cache_dir=args.cache_dir,
            output_dir=args.output_dir,
            symbols=symbols,
            n_trades=args.n_trades,
            seed=args.seed,
            trade_date=trade_date,
        )
        if args.max_corporate_action_breaks is not None:
            config.max_corporate_action_breaks = args.max_corporate_action_breaks
        summary = run_generate(config)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
