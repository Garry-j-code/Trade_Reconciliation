"""Deterministic matching predicates and break-type vocabulary.

Pure functions — no LLM, no network, no DB I/O.

Matching strategy (see ``backend.pipeline.matcher``)
---------------------------------------------------
1. **Exact pass** — same symbol, side, trade_date, settlement_date, quantity,
   and price (float-safe equality).
2. **Tolerance pass** — same symbol / side / trade_date / settlement, quantity
   exact, price within ``DEFAULT_PRICE_TOLERANCE_BPS`` (5 bps). Injected
   generator price breaks are 75 bps, so they stay breaks.
3. **Corporate-action pass** — quantity/price ratio matches a cached split
   factor near ``execution_date`` → treat as a match, not a qty/price break.
4. **Split-fill pass** — one desk block vs 2+ broker fills whose quantities
   sum to the desk quantity (prices within the bps band).

Leftovers become breaks: missing trade, quantity, price, duplicate,
settlement-date mismatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import pandas as pd

# ---------------------------------------------------------------------------
# Match passes (written to matches.match_pass)
# ---------------------------------------------------------------------------

MATCH_PASS_EXACT = "exact"
MATCH_PASS_TOLERANCE = "tolerance"
MATCH_PASS_SPLIT_FILL = "split_fill"
MATCH_PASS_CORPORATE_ACTION = "corporate_action"

MATCH_PASSES: tuple[str, ...] = (
    MATCH_PASS_EXACT,
    MATCH_PASS_TOLERANCE,
    MATCH_PASS_SPLIT_FILL,
    MATCH_PASS_CORPORATE_ACTION,
)

# ---------------------------------------------------------------------------
# Break types — strings align with backend.data.generator ground truth
# ---------------------------------------------------------------------------

BREAK_MISSING_BROKER = "missing_broker"
BREAK_MISSING_DESK = "missing_desk"
BREAK_PRICE = "price_break"
BREAK_QUANTITY = "quantity_break"
BREAK_DUPLICATE = "duplicate"
BREAK_SETTLEMENT = "settlement_date_mismatch"
BREAK_SPLIT_FILL = "split_fill"  # ground-truth label; matcher records a match

BREAK_TYPES: tuple[str, ...] = (
    BREAK_MISSING_BROKER,
    BREAK_MISSING_DESK,
    BREAK_PRICE,
    BREAK_QUANTITY,
    BREAK_DUPLICATE,
    BREAK_SETTLEMENT,
)

BREAK_STATUS_OPEN = "open"

# 5 bps = 0.05%. Generator injected price breaks are 75 bps, so they miss
# this band. Tiny rounding / venue diffs still tolerance-match.
DEFAULT_PRICE_TOLERANCE_BPS: float = 5.0
DEFAULT_QTY_ABS_TOL: float = 1e-6
DEFAULT_EXACT_PRICE_ABS_TOL: float = 1e-4  # $0.0001
DEFAULT_CA_WINDOW_DAYS: int = 14
DEFAULT_CA_RATIO_REL_TOL: float = 1e-3  # 0.1%
DEFAULT_NOTIONAL_REL_TOL: float = 1e-3


SPLITS_COLUMNS: tuple[str, ...] = (
    "ticker",
    "execution_date",
    "split_from",
    "split_to",
)


@dataclass(frozen=True)
class SplitHit:
    """Cached split that could explain a qty/price ratio near a trade date."""

    symbol: str
    execution_date: date
    split_from: float
    split_to: float
    ratio: float  # split_to / split_from (broker-adjusted factor)


def as_date(value: Any) -> date | None:
    """Coerce date-like values to ``datetime.date``; invalid / NaT → None."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.date()
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def as_datetime(value: Any) -> datetime | None:
    """Coerce to timezone-aware UTC ``datetime``; invalid / NaT → None."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    dt: datetime | None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        dt = value.to_pydatetime()
    else:
        parsed = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(parsed):
            return None
        dt = parsed.to_pydatetime()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def price_diff_bps(price_a: float, price_b: float) -> float:
    """Absolute price difference in basis points vs the mid price."""
    a = float(price_a)
    b = float(price_b)
    mid = (abs(a) + abs(b)) / 2.0
    if mid == 0.0:
        return 0.0 if a == b else float("inf")
    return abs(a - b) / mid * 10_000.0


def price_within_tolerance(
    price_a: float,
    price_b: float,
    bps: float = DEFAULT_PRICE_TOLERANCE_BPS,
) -> bool:
    """True when the two prices differ by at most ``bps`` basis points."""
    if bps < 0:
        raise ValueError("bps must be >= 0")
    return price_diff_bps(price_a, price_b) <= float(bps)


def quantities_equal(
    qty_a: float,
    qty_b: float,
    abs_tol: float = DEFAULT_QTY_ABS_TOL,
) -> bool:
    """True when quantities differ by at most ``abs_tol`` shares."""
    return abs(float(qty_a) - float(qty_b)) <= float(abs_tol)


def settlements_equal(settle_a: Any, settle_b: Any) -> bool:
    """True when both settlement dates parse and are the same calendar day."""
    da = as_date(settle_a)
    db = as_date(settle_b)
    if da is None or db is None:
        return False
    return da == db


def exact_match_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Hashable exact-pass key: symbol, side, trade_date, qty, price, settle."""
    qty = float(row["quantity"])
    px = float(row["price"])
    return (
        str(row["symbol"]).upper(),
        str(row["side"]).upper(),
        as_date(row["trade_date"]),
        round(qty, 6),
        round(px, 4),
        as_date(row["settlement_date"]),
    )


def group_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Coarse key used to bucket candidate pairs: symbol + side + trade_date."""
    return (
        str(row["symbol"]).upper(),
        str(row["side"]).upper(),
        as_date(row["trade_date"]),
    )


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


def empty_splits_frame() -> pd.DataFrame:
    """Empty splits frame with the cache column set."""
    return pd.DataFrame(columns=list(SPLITS_COLUMNS))


def _row_symbol(row: Mapping[str, Any]) -> str:
    if "ticker" in row and row["ticker"] is not None and not (
        isinstance(row["ticker"], float) and pd.isna(row["ticker"])
    ):
        return str(row["ticker"]).upper()
    if "symbol" in row and row["symbol"] is not None:
        return str(row["symbol"]).upper()
    return ""


def find_split_hit(
    splits: pd.DataFrame | None,
    symbol: str,
    trade_date: Any,
    *,
    window_days: int = DEFAULT_CA_WINDOW_DAYS,
) -> SplitHit | None:
    """Closest cached split for ``symbol`` within ``window_days`` of trade_date.

    Returns None when no splits are loaded or none fall in the window.
    """
    if splits is None or splits.empty:
        return None
    td = as_date(trade_date)
    if td is None:
        return None
    want = str(symbol).upper()
    window = timedelta(days=int(window_days))
    best: SplitHit | None = None
    best_delta: int | None = None
    for raw in splits.to_dict(orient="records"):
        sym = _row_symbol(raw)
        if sym != want:
            continue
        exec_date = as_date(raw.get("execution_date"))
        if exec_date is None:
            continue
        delta = abs((exec_date - td).days)
        if delta > window.days:
            continue
        ratio = split_ratio(raw.get("split_from"), raw.get("split_to"))
        if ratio is None or ratio == 1.0:
            continue
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = SplitHit(
                symbol=sym,
                execution_date=exec_date,
                split_from=float(raw["split_from"]),
                split_to=float(raw["split_to"]),
                ratio=ratio,
            )
    return best


def _close_ratio(observed: float, expected: float, rel_tol: float) -> bool:
    if expected == 0:
        return False
    return abs(observed - expected) / abs(expected) <= float(rel_tol)


def is_corporate_action_adjusted(
    broker_qty: float,
    broker_price: float,
    desk_qty: float,
    desk_price: float,
    factor: float,
    *,
    ratio_rel_tol: float = DEFAULT_CA_RATIO_REL_TOL,
    notional_rel_tol: float = DEFAULT_NOTIONAL_REL_TOL,
) -> bool:
    """True when qty/price ratios match ``factor`` (or 1/factor) and notionals agree.

    Broker-adjusted (generator model): broker_qty / desk_qty ≈ factor and
    desk_price / broker_price ≈ factor. The inverse (desk-adjusted) is also
    accepted so a lag on either side still classifies as CA, not a break.
    """
    bq, dq = float(broker_qty), float(desk_qty)
    bp, dp = float(broker_price), float(desk_price)
    if bq == 0 or dq == 0 or bp == 0 or dp == 0 or factor == 0:
        return False
    qty_ratio = bq / dq
    px_ratio = dp / bp
    broker_adj = _close_ratio(qty_ratio, factor, ratio_rel_tol) and _close_ratio(
        px_ratio, factor, ratio_rel_tol
    )
    desk_adj = _close_ratio(qty_ratio, 1.0 / factor, ratio_rel_tol) and _close_ratio(
        px_ratio, 1.0 / factor, ratio_rel_tol
    )
    if not (broker_adj or desk_adj):
        return False
    broker_notional = bq * bp
    desk_notional = dq * dp
    mid = (abs(broker_notional) + abs(desk_notional)) / 2.0
    if mid == 0:
        return False
    return abs(broker_notional - desk_notional) / mid <= float(notional_rel_tol)


def select_fill_indices(
    quantities: Sequence[float],
    target: float,
    *,
    abs_tol: float = DEFAULT_QTY_ABS_TOL,
    min_fills: int = 2,
) -> tuple[int, ...] | None:
    """Indices of 2+ fills whose quantities sum to ``target``.

    Prefers using every fill when the full sum matches (the generator's
    one-desk-block vs all-broker-fills shape). Otherwise the smallest
    matching subset of size >= ``min_fills``.
    """
    n = len(quantities)
    if n < min_fills:
        return None
    qtys = [float(q) for q in quantities]
    if abs(sum(qtys) - float(target)) <= float(abs_tol):
        return tuple(range(n))
    best: tuple[int, ...] | None = None
    for mask in range(1, 1 << n):
        idxs = tuple(i for i in range(n) if mask & (1 << i))
        if len(idxs) < min_fills:
            continue
        total = sum(qtys[i] for i in idxs)
        if abs(total - float(target)) > float(abs_tol):
            continue
        if (
            best is None
            or len(idxs) < len(best)
            or (len(idxs) == len(best) and idxs < best)
        ):
            best = idxs
    return best


def join_trade_ids(ids: Sequence[str]) -> str:
    """Join trade ids with commas (empty sequence → empty string)."""
    return ",".join(str(i) for i in ids if i)


def parse_trade_ids(value: Any) -> list[str]:
    """Split a comma-separated trade-id string; None/NaN → []."""
    if value is None:
        return []
    if isinstance(value, float) and pd.isna(value):
        return []
    text = str(value).strip()
    if not text:
        return []
    return [part for part in (p.strip() for p in text.split(",")) if part]
