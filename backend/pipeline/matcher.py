"""Deterministic broker↔desk matching. Pure functions over dataframes.

No LLM, no network. Optional splits frame is a local Parquet cache (never fetched live).

Pass order (see ``backend.pipeline.rules``):
  exact → price-tolerance → retract duplicates → corporate-action → split-fill
  → field-level breaks on leftover pairs → one-sided missing trades.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.data.fetch_market_data import default_cache_dir
from backend.db.models import Break, Match, NormalizedTrade
from backend.db.session import (
    create_all_tables,
    database_url_from_env,
    get_engine,
    get_session_factory,
    session_scope,
)
from backend.pipeline.ingest import NORMALIZED_FILENAME, default_normalized_output_dir
from backend.pipeline.normalize import CANONICAL_COLUMNS, SOURCE_BROKER, SOURCE_DESK
from backend.pipeline.rules import (
    BREAK_DUPLICATE,
    BREAK_MISSING_BROKER,
    BREAK_MISSING_DESK,
    BREAK_PRICE,
    BREAK_QUANTITY,
    BREAK_SETTLEMENT,
    BREAK_STATUS_OPEN,
    DEFAULT_PRICE_TOLERANCE_BPS,
    DEFAULT_QTY_ABS_TOL,
    MATCH_PASS_CORPORATE_ACTION,
    MATCH_PASS_EXACT,
    MATCH_PASS_SPLIT_FILL,
    MATCH_PASS_TOLERANCE,
    as_date,
    empty_splits_frame,
    exact_match_key,
    find_split_hit,
    group_key,
    is_corporate_action_adjusted,
    join_trade_ids,
    price_within_tolerance,
    quantities_equal,
    select_fill_indices,
    settlements_equal,
)

BREAK_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

logger = logging.getLogger(__name__)

MATCHES_FILENAME = "matches.parquet"
BREAKS_FILENAME = "breaks.parquet"
REQUIRED_MATCH_COLUMNS: tuple[str, ...] = (
    "trade_id",
    "source",
    "symbol",
    "trade_date",
    "settlement_date",
    "side",
    "quantity",
    "price",
)


class MatchError(ValueError):
    """Raised when matcher input is missing columns or invalid."""


MATCH_COLUMNS: tuple[str, ...] = (
    "broker_trade_id",
    "desk_trade_id",
    "pair_id",
    "match_pass",
)

BREAK_COLUMNS: tuple[str, ...] = (
    "break_type",
    "status",
    "pair_id",
    "broker_trade_ids",
    "desk_trade_ids",
    "symbol",
    "trade_date",
    "detail",
)


def empty_matches_frame() -> pd.DataFrame:
    """Empty matches frame with the persist column set."""
    return pd.DataFrame(columns=list(MATCH_COLUMNS))


def empty_breaks_frame() -> pd.DataFrame:
    """Empty breaks frame with the persist column set."""
    return pd.DataFrame(columns=list(BREAK_COLUMNS))


def load_splits_cache(cache_dir: Path | None = None) -> pd.DataFrame:
    """Read ``splits.parquet`` from the local market-data cache (no network)."""
    root = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    path = root / "splits.parquet"
    if not path.exists():
        return empty_splits_frame()
    return pd.read_parquet(path)


def break_identity(row: Mapping[str, Any]) -> str:
    """Stable key so rematch can reuse ``break_id`` (keeps suggestions + audit)."""
    td = as_date(row.get("trade_date"))
    return "|".join(
        [
            str(row.get("break_type") or ""),
            str(row.get("pair_id") or ""),
            str(row.get("broker_trade_ids") or ""),
            str(row.get("desk_trade_ids") or ""),
            td.isoformat() if td else "",
        ]
    )


def stable_break_id(row: Mapping[str, Any]) -> uuid.UUID:
    """uuid5 from ``break_identity`` — same blotter row → same id."""
    return uuid.uuid5(BREAK_ID_NAMESPACE, break_identity(row))


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def _trade_date(row: Mapping[str, Any]) -> date | None:
    return as_date(row.get("trade_date"))


def _notional(row: Mapping[str, Any]) -> float:
    return abs(float(row["quantity"]) * float(row["price"]))


def _shared_pair_id(rows: Sequence[Mapping[str, Any]]) -> str | None:
    ids = {_opt_str(r.get("pair_id")) for r in rows}
    ids.discard(None)
    if len(ids) == 1:
        return next(iter(ids))
    return None


def _desk_code(desk_rows: Sequence[Mapping[str, Any]]) -> str | None:
    for row in desk_rows:
        account = _opt_str(row.get("account"))
        if account:
            return account
    return None


def _notional_at_risk(
    broker_rows: Sequence[Mapping[str, Any]],
    desk_rows: Sequence[Mapping[str, Any]],
) -> float:
    broker_n = sum(_notional(r) for r in broker_rows)
    desk_n = sum(_notional(r) for r in desk_rows)
    if broker_rows and desk_rows:
        return max(broker_n, desk_n)
    return broker_n + desk_n


def _match_row(
    broker: Mapping[str, Any],
    desk: Mapping[str, Any],
    match_pass: str,
) -> dict[str, Any]:
    return {
        "broker_trade_id": str(broker["trade_id"]),
        "desk_trade_id": str(desk["trade_id"]),
        "pair_id": _shared_pair_id([broker, desk]),
        "match_pass": match_pass,
    }


def _break_row(
    break_type: str,
    broker_rows: Sequence[Mapping[str, Any]],
    desk_rows: Sequence[Mapping[str, Any]],
    *,
    extra_detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    all_rows = list(broker_rows) + list(desk_rows)
    symbol = None
    trade_dt = None
    if all_rows:
        symbol = _opt_str(all_rows[0].get("symbol"))
        if symbol:
            symbol = symbol.upper()
        trade_dt = _trade_date(all_rows[0])
    broker_ids = [str(r["trade_id"]) for r in broker_rows]
    desk_ids = [str(r["trade_id"]) for r in desk_rows]
    detail: dict[str, Any] = {
        "desk": _desk_code(desk_rows),
        "notional_at_risk": _notional_at_risk(broker_rows, desk_rows),
        "broker_trade_ids": broker_ids,
        "desk_trade_ids": desk_ids,
    }
    if extra_detail:
        detail.update(dict(extra_detail))
    return {
        "break_type": break_type,
        "status": BREAK_STATUS_OPEN,
        "pair_id": _shared_pair_id(all_rows),
        "broker_trade_ids": join_trade_ids(broker_ids),
        "desk_trade_ids": join_trade_ids(desk_ids),
        "symbol": symbol,
        "trade_date": trade_dt,
        "detail": detail,
    }


def _source_rows(normalized: pd.DataFrame, source: str) -> list[dict[str, Any]]:
    if normalized.empty or "source" not in normalized.columns:
        return []
    subset = normalized[normalized["source"].astype(str).str.lower() == source]
    return [dict(r) for r in subset.to_dict(orient="records")]


def _classify_mismatch(
    broker: Mapping[str, Any],
    desk: Mapping[str, Any],
    *,
    price_tolerance_bps: float,
    qty_abs_tol: float,
) -> str:
    qty_ok = quantities_equal(
        broker["quantity"], desk["quantity"], abs_tol=qty_abs_tol
    )
    px_ok = price_within_tolerance(
        broker["price"], desk["price"], bps=price_tolerance_bps
    )
    settle_ok = settlements_equal(
        broker["settlement_date"], desk["settlement_date"]
    )
    if qty_ok and px_ok and not settle_ok:
        return BREAK_SETTLEMENT
    if qty_ok and settle_ok and not px_ok:
        return BREAK_PRICE
    if px_ok and settle_ok and not qty_ok:
        return BREAK_QUANTITY
    if not qty_ok:
        return BREAK_QUANTITY
    if not px_ok:
        return BREAK_PRICE
    return BREAK_SETTLEMENT


def _greedy_notional_pairs(
    broker: Sequence[Mapping[str, Any]],
    desk: Sequence[Mapping[str, Any]],
    broker_idxs: Sequence[int],
    desk_idxs: Sequence[int],
) -> list[tuple[int, int]]:
    candidates: list[tuple[float, int, int]] = []
    for bi in broker_idxs:
        nb = _notional(broker[bi])
        for di in desk_idxs:
            nd = _notional(desk[di])
            candidates.append((abs(nb - nd), bi, di))
    candidates.sort(key=lambda t: (t[0], t[1], t[2]))
    used_b: set[int] = set()
    used_d: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for _, bi, di in candidates:
        if bi in used_b or di in used_d:
            continue
        used_b.add(bi)
        used_d.add(di)
        pairs.append((bi, di))
    return pairs


def match_normalized_trades(
    normalized: pd.DataFrame,
    splits: pd.DataFrame | None = None,
    *,
    price_tolerance_bps: float = DEFAULT_PRICE_TOLERANCE_BPS,
    qty_abs_tol: float = DEFAULT_QTY_ABS_TOL,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Match canonical broker/desk rows. Returns ``(matches_df, breaks_df)``.

    Pass order: exact → tolerance → retract duplicates → corporate-action
    → split-fill → field-level breaks → one-sided missing trades.
    ``pair_id`` is preserved on output when present but is never a match key.
    """
    missing = [c for c in REQUIRED_MATCH_COLUMNS if c not in normalized.columns]
    if missing:
        raise MatchError(f"normalized frame missing columns: {', '.join(missing)}")
    broker = _source_rows(normalized, SOURCE_BROKER)
    desk = _source_rows(normalized, SOURCE_DESK)
    b_used = [False] * len(broker)
    d_used = [False] * len(desk)
    matches: list[dict[str, Any]] = []
    breaks: list[dict[str, Any]] = []

    desk_by_exact: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for di, drow in enumerate(desk):
        desk_by_exact[exact_match_key(drow)].append(di)

    # 1. Exact 1:1
    for bi, brow in enumerate(broker):
        key = exact_match_key(brow)
        for di in desk_by_exact.get(key, []):
            if d_used[di]:
                continue
            d_used[di] = True
            b_used[bi] = True
            matches.append(
                {**_match_row(brow, desk[di], MATCH_PASS_EXACT), "_bi": bi, "_di": di}
            )
            break

    # 2. Tolerance 1:1 (same settle + qty; price within bps)
    for bi, brow in enumerate(broker):
        if b_used[bi]:
            continue
        b_group = group_key(brow)
        for di, drow in enumerate(desk):
            if d_used[di]:
                continue
            if group_key(drow) != b_group:
                continue
            if not quantities_equal(
                brow["quantity"], drow["quantity"], abs_tol=qty_abs_tol
            ):
                continue
            if not settlements_equal(brow["settlement_date"], drow["settlement_date"]):
                continue
            if not price_within_tolerance(
                brow["price"], drow["price"], bps=price_tolerance_bps
            ):
                continue
            d_used[di] = True
            b_used[bi] = True
            matches.append(
                {
                    **_match_row(brow, drow, MATCH_PASS_TOLERANCE),
                    "_bi": bi,
                    "_di": di,
                }
            )
            break

    # 3. Duplicate extras sharing an exact key with a 1:1 match → break, not match
    key_to_match_idxs: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for i, m in enumerate(matches):
        key_to_match_idxs[exact_match_key(broker[int(m["_bi"])])].append(i)

    extra_by_key: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for bi, used in enumerate(b_used):
        if used:
            continue
        key = exact_match_key(broker[bi])
        if key_to_match_idxs.get(key):
            extra_by_key[key].append(bi)

    retract: set[int] = set()
    for key, extra_idxs in extra_by_key.items():
        broker_rows: list[Mapping[str, Any]] = []
        desk_rows: list[Mapping[str, Any]] = []
        for mi in key_to_match_idxs[key]:
            retract.add(mi)
            m = matches[mi]
            broker_rows.append(broker[int(m["_bi"])])
            desk_rows.append(desk[int(m["_di"])])
        for bi in extra_idxs:
            b_used[bi] = True
            broker_rows.append(broker[bi])
        breaks.append(
            _break_row(
                BREAK_DUPLICATE,
                broker_rows,
                desk_rows,
                extra_detail={"reason": "extra broker booking of the same economic trade"},
            )
        )
    matches = [m for i, m in enumerate(matches) if i not in retract]

    # 4. Corporate-action 1:1 (cached split explains qty/price ratio)
    for bi, brow in enumerate(broker):
        if b_used[bi]:
            continue
        hit = find_split_hit(splits, str(brow["symbol"]), brow.get("trade_date"))
        if hit is None:
            continue
        b_group = group_key(brow)
        for di, drow in enumerate(desk):
            if d_used[di]:
                continue
            if group_key(drow) != b_group:
                continue
            if not is_corporate_action_adjusted(
                float(brow["quantity"]),
                float(brow["price"]),
                float(drow["quantity"]),
                float(drow["price"]),
                hit.ratio,
            ):
                continue
            d_used[di] = True
            b_used[bi] = True
            matches.append(
                {
                    **_match_row(brow, drow, MATCH_PASS_CORPORATE_ACTION),
                    "_bi": bi,
                    "_di": di,
                }
            )
            break

    # 5. Split-fill: one unused desk vs 2+ unused broker fills
    for di, drow in enumerate(desk):
        if d_used[di]:
            continue
        d_group = group_key(drow)
        candidates: list[int] = []
        for bi, brow in enumerate(broker):
            if b_used[bi]:
                continue
            if group_key(brow) != d_group:
                continue
            if not settlements_equal(brow["settlement_date"], drow["settlement_date"]):
                continue
            if not price_within_tolerance(
                brow["price"], drow["price"], bps=price_tolerance_bps
            ):
                continue
            candidates.append(bi)
        if len(candidates) < 2:
            continue
        qtys = [float(broker[bi]["quantity"]) for bi in candidates]
        chosen = select_fill_indices(
            qtys, float(drow["quantity"]), abs_tol=qty_abs_tol
        )
        if chosen is None:
            continue
        d_used[di] = True
        for local_i in chosen:
            bi = candidates[local_i]
            b_used[bi] = True
            matches.append(
                {
                    **_match_row(broker[bi], drow, MATCH_PASS_SPLIT_FILL),
                    "_bi": bi,
                    "_di": di,
                }
            )

    # 6. Remaining same-group leftovers → field-level breaks
    b_groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    d_groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for bi, used in enumerate(b_used):
        if not used:
            b_groups[group_key(broker[bi])].append(bi)
    for di, used in enumerate(d_used):
        if not used:
            d_groups[group_key(desk[di])].append(di)

    for key in set(b_groups) | set(d_groups):
        pairs = _greedy_notional_pairs(
            broker, desk, b_groups.get(key, []), d_groups.get(key, [])
        )
        for bi, di in pairs:
            b_used[bi] = True
            d_used[di] = True
            brow = broker[bi]
            drow = desk[di]
            breaks.append(
                _break_row(
                    _classify_mismatch(
                        brow,
                        drow,
                        price_tolerance_bps=price_tolerance_bps,
                        qty_abs_tol=qty_abs_tol,
                    ),
                    [brow],
                    [drow],
                )
            )

    # 7. One-sided leftovers
    for bi, used in enumerate(b_used):
        if not used:
            breaks.append(_break_row(BREAK_MISSING_DESK, [broker[bi]], []))
    for di, used in enumerate(d_used):
        if not used:
            breaks.append(_break_row(BREAK_MISSING_BROKER, [], [desk[di]]))

    matches_df = (
        pd.DataFrame(
            [{k: m[k] for k in MATCH_COLUMNS} for m in matches],
            columns=list(MATCH_COLUMNS),
        )
        if matches
        else empty_matches_frame()
    )
    breaks_df = (
        pd.DataFrame(breaks, columns=list(BREAK_COLUMNS))
        if breaks
        else empty_breaks_frame()
    )
    return matches_df.reset_index(drop=True), breaks_df.reset_index(drop=True)


def split_legs(normalized: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a canonical frame into broker and desk legs."""
    if "source" not in normalized.columns:
        raise MatchError("normalized frame missing column: source")
    if normalized.empty:
        empty = normalized.iloc[0:0].copy()
        return empty, empty
    src = normalized["source"].astype(str).str.lower()
    broker = normalized[src == SOURCE_BROKER].copy().reset_index(drop=True)
    desk = normalized[src == SOURCE_DESK].copy().reset_index(drop=True)
    return broker, desk


def summarize_match_frames(
    matches: pd.DataFrame, breaks: pd.DataFrame
) -> dict[str, Any]:
    """Count rows by match_pass and break_type."""
    match_counts: dict[str, int] = {}
    if not matches.empty and "match_pass" in matches.columns:
        match_counts = {
            str(k): int(v) for k, v in matches["match_pass"].value_counts().items()
        }
    break_counts: dict[str, int] = {}
    if not breaks.empty and "break_type" in breaks.columns:
        break_counts = {
            str(k): int(v) for k, v in breaks["break_type"].value_counts().items()
        }
    return {
        "match_rows": int(len(matches)),
        "break_rows": int(len(breaks)),
        "match_pass_counts": match_counts,
        "break_type_counts": break_counts,
    }


@dataclass
class MatchResult:
    matches: pd.DataFrame
    breaks: pd.DataFrame
    summary: dict[str, Any]


def match_trades(
    normalized: pd.DataFrame,
    splits: pd.DataFrame | None = None,
    *,
    price_tolerance_bps: float = DEFAULT_PRICE_TOLERANCE_BPS,
    qty_abs_tol: float = DEFAULT_QTY_ABS_TOL,
) -> MatchResult:
    """Wrapper around ``match_normalized_trades`` that adds a summary dict."""
    matches, breaks = match_normalized_trades(
        normalized,
        splits,
        price_tolerance_bps=price_tolerance_bps,
        qty_abs_tol=qty_abs_tol,
    )
    return MatchResult(
        matches=matches,
        breaks=breaks,
        summary=summarize_match_frames(matches, breaks),
    )


# ---------------------------------------------------------------------------
# I/O — Parquet + optional RDS (same pattern as normalize-trades)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchPaths:
    normalized_dir: Path
    output_dir: Path
    cache_dir: Path
    normalized: Path
    matches: Path
    breaks: Path
    splits: Path


def default_matched_output_dir() -> Path:
    override = os.environ.get("MATCHED_OUTPUT_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "data" / "matched"


def resolve_match_paths(
    *,
    normalized_dir: Path | None = None,
    output_dir: Path | None = None,
    cache_dir: Path | None = None,
) -> MatchPaths:
    norm_dir = (
        Path(normalized_dir)
        if normalized_dir is not None
        else default_normalized_output_dir()
    )
    out = Path(output_dir) if output_dir is not None else default_matched_output_dir()
    cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    return MatchPaths(
        normalized_dir=norm_dir,
        output_dir=out,
        cache_dir=cache,
        normalized=norm_dir / NORMALIZED_FILENAME,
        matches=out / MATCHES_FILENAME,
        breaks=out / BREAKS_FILENAME,
        splits=cache / "splits.parquet",
    )


def read_normalized_parquet(path: Path) -> pd.DataFrame:
    """Read canonical trades written by ``normalize-trades``."""
    if not path.exists():
        raise FileNotFoundError(f"Missing normalized trades: {path}")
    return pd.read_parquet(path)


def _json_cell(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def prepare_matches_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    """Copy matches; add ``match_id`` when missing."""
    out = df.copy()
    if "match_id" not in out.columns:
        out.insert(
            0,
            "match_id",
            [str(uuid.uuid4()) for _ in range(len(out))],
        )
    cols = ["match_id", *MATCH_COLUMNS]
    for col in cols:
        if col not in out.columns:
            out[col] = None
    return out[cols].reset_index(drop=True)


def prepare_breaks_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    """Copy breaks with JSON-string ``detail``, ISO dates, and ids."""
    out = df.copy()
    if "break_id" not in out.columns:
        ids = [
            str(stable_break_id(r)) for r in out.to_dict(orient="records")
        ]
        out.insert(0, "break_id", ids)
    if "cluster_id" not in out.columns:
        out["cluster_id"] = None
    for col in BREAK_COLUMNS:
        if col not in out.columns:
            out[col] = None
    out["detail"] = out["detail"].map(_json_cell)
    out["trade_date"] = out["trade_date"].map(
        lambda d: d.isoformat() if hasattr(d, "isoformat") else d
    )
    out["cluster_id"] = out["cluster_id"].map(
        lambda v: None
        if v is None or (isinstance(v, float) and pd.isna(v))
        else str(v)
    )
    cols = ["break_id", *BREAK_COLUMNS, "cluster_id"]
    return out[cols].reset_index(drop=True)


def write_matches_parquet(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    prepare_matches_for_parquet(df).to_parquet(path, index=False)
    return path


def write_breaks_parquet(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    prepare_breaks_for_parquet(df).to_parquet(path, index=False)
    return path


def _as_uuid(value: Any) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def match_row_to_orm(row: Mapping[str, Any]) -> Match:
    pair = row.get("pair_id")
    pair_id = (
        None
        if pair is None or (isinstance(pair, float) and pd.isna(pair))
        else str(pair)
    )
    kwargs: dict[str, Any] = {
        "broker_trade_id": str(row["broker_trade_id"]),
        "desk_trade_id": str(row["desk_trade_id"]),
        "pair_id": pair_id,
        "match_pass": str(row["match_pass"]),
    }
    if row.get("match_id") is not None and not (
        isinstance(row.get("match_id"), float) and pd.isna(row.get("match_id"))
    ):
        kwargs["match_id"] = _as_uuid(row["match_id"])
    return Match(**kwargs)


def break_row_to_orm(row: Mapping[str, Any]) -> Break:
    pair = row.get("pair_id")
    pair_id = (
        None
        if pair is None or (isinstance(pair, float) and pd.isna(pair))
        else str(pair)
    )
    detail = row.get("detail")
    if isinstance(detail, str):
        detail = json.loads(detail) if detail else None
    elif detail is not None and not isinstance(detail, dict):
        detail = None
    cluster = row.get("cluster_id")
    cluster_id = None
    if cluster is not None and not (isinstance(cluster, float) and pd.isna(cluster)):
        cluster_id = _as_uuid(cluster)
    kwargs: dict[str, Any] = {
        "break_type": str(row["break_type"]),
        "status": str(row.get("status") or BREAK_STATUS_OPEN),
        "pair_id": pair_id,
        "broker_trade_ids": (
            None
            if row.get("broker_trade_ids") is None
            or (
                isinstance(row.get("broker_trade_ids"), float)
                and pd.isna(row.get("broker_trade_ids"))
            )
            else str(row["broker_trade_ids"])
        ),
        "desk_trade_ids": (
            None
            if row.get("desk_trade_ids") is None
            or (
                isinstance(row.get("desk_trade_ids"), float)
                and pd.isna(row.get("desk_trade_ids"))
            )
            else str(row["desk_trade_ids"])
        ),
        "symbol": None if row.get("symbol") is None else str(row["symbol"]),
        "trade_date": as_date(row.get("trade_date")),
        "detail": detail,
        "cluster_id": cluster_id,
    }
    if row.get("break_id") is not None and not (
        isinstance(row.get("break_id"), float) and pd.isna(row.get("break_id"))
    ):
        kwargs["break_id"] = _as_uuid(row["break_id"])
    return Break(**kwargs)


def load_frames_to_db(
    matches: pd.DataFrame,
    breaks: pd.DataFrame,
    session: Session,
    *,
    replace: bool = True,
    preserve_audit: bool = True,
) -> dict[str, int]:
    """Insert match/break rows.

    When ``replace``, rebuild ``matches`` and ``breaks``. ``audit_log`` is never
    deleted. Existing breaks with the same identity keep their ``break_id``
    (and therefore ``resolution_suggestions``). Stale breaks are deleted;
    ``audit_log.break_id`` is SET NULL by the FK.
    """
    _ = preserve_audit
    prepared_m = prepare_matches_for_parquet(matches)
    prepared_b = prepare_breaks_for_parquet(breaks)

    existing_by_key: dict[str, Break] = {}
    if replace:
        for row in session.query(Break).all():
            existing_by_key[
                break_identity(
                    {
                        "break_type": row.break_type,
                        "pair_id": row.pair_id,
                        "broker_trade_ids": row.broker_trade_ids,
                        "desk_trade_ids": row.desk_trade_ids,
                        "trade_date": row.trade_date,
                    }
                )
            ] = row

        session.execute(delete(Match))

        keep_ids: set[uuid.UUID] = set()
        break_orms: list[Break] = []
        for rec in prepared_b.to_dict(orient="records"):
            key = break_identity(rec)
            old = existing_by_key.get(key)
            if old is not None:
                rec["break_id"] = str(old.break_id)
            orm = break_row_to_orm(rec)
            if old is not None:
                orm.status = old.status
                orm.cluster_id = old.cluster_id
            keep_ids.add(orm.break_id)
            break_orms.append(orm)

        stale_ids = [
            row.break_id
            for row in existing_by_key.values()
            if row.break_id not in keep_ids
        ]
        if stale_ids:
            session.execute(delete(Break).where(Break.break_id.in_(stale_ids)))

        for orm in break_orms:
            current = session.get(Break, orm.break_id)
            if current is None:
                session.add(orm)
                continue
            current.break_type = orm.break_type
            current.pair_id = orm.pair_id
            current.broker_trade_ids = orm.broker_trade_ids
            current.desk_trade_ids = orm.desk_trade_ids
            current.symbol = orm.symbol
            current.trade_date = orm.trade_date
            current.detail = orm.detail
            if current.status == BREAK_STATUS_OPEN:
                current.status = orm.status

        match_orms = [match_row_to_orm(r) for r in prepared_m.to_dict(orient="records")]
        session.add_all(match_orms)
        session.flush()
        return {"matches": len(match_orms), "breaks": len(break_orms)}

    match_orms = [match_row_to_orm(r) for r in prepared_m.to_dict(orient="records")]
    break_orms = [break_row_to_orm(r) for r in prepared_b.to_dict(orient="records")]
    session.add_all(match_orms)
    session.add_all(break_orms)
    session.flush()
    return {"matches": len(match_orms), "breaks": len(break_orms)}


def load_to_database(
    matches: pd.DataFrame,
    breaks: pd.DataFrame,
    database_url: str,
    *,
    replace: bool = True,
) -> dict[str, int]:
    """Create schema if needed and load matches/breaks into Postgres."""
    engine = get_engine(database_url)
    create_all_tables(engine)
    factory = get_session_factory(engine)
    with session_scope(factory) as session:
        counts = load_frames_to_db(matches, breaks, session, replace=replace)
    return counts


def normalized_orm_to_record(row: NormalizedTrade) -> dict[str, Any]:
    """Map a ``NormalizedTrade`` ORM row to a canonical dict."""
    return {
        "trade_id": row.trade_id,
        "source": row.source,
        "symbol": row.symbol,
        "trade_date": row.trade_date,
        "settlement_date": row.settlement_date,
        "side": row.side,
        "quantity": row.quantity,
        "price": row.price,
        "currency": row.currency,
        "account": row.account,
        "executing_party": row.executing_party,
        "pair_id": row.pair_id,
        "raw_payload": row.raw_payload,
    }


def read_normalized_from_db(database_url: str) -> pd.DataFrame:
    """Load canonical trades from RDS ``normalized_trades``."""
    engine = get_engine(database_url)
    factory = get_session_factory(engine)
    with session_scope(factory) as session:
        rows = session.query(NormalizedTrade).all()
        records = [normalized_orm_to_record(r) for r in rows]
    if not records:
        return pd.DataFrame(columns=list(CANONICAL_COLUMNS))
    return pd.DataFrame(records)


@dataclass
class MatchRunResult:
    match_rows: int
    break_rows: int
    summary: dict[str, Any]
    matches_path: Path
    breaks_path: Path
    db_loaded: bool
    db_counts: dict[str, int] | None
    source: str


def run_match(
    *,
    normalized_dir: Path | None = None,
    output_dir: Path | None = None,
    cache_dir: Path | None = None,
    database_url: str | None = None,
    write_parquet: bool = True,
    load_db: bool | None = None,
    from_db: bool = False,
    price_tolerance_bps: float = DEFAULT_PRICE_TOLERANCE_BPS,
) -> MatchRunResult:
    """Read normalized trades → match → write Parquet; optionally load DB."""
    paths = resolve_match_paths(
        normalized_dir=normalized_dir,
        output_dir=output_dir,
        cache_dir=cache_dir,
    )
    url = database_url if database_url is not None else database_url_from_env()
    source = "parquet"
    if from_db:
        if not url:
            raise ValueError("from_db requested but DATABASE_URL is not set")
        normalized = read_normalized_from_db(url)
        source = "rds"
    else:
        normalized = read_normalized_parquet(paths.normalized)

    splits = load_splits_cache(paths.cache_dir)
    result = match_trades(
        normalized, splits=splits, price_tolerance_bps=price_tolerance_bps
    )

    if write_parquet:
        write_matches_parquet(result.matches, paths.matches)
        write_breaks_parquet(result.breaks, paths.breaks)
        logger.info(
            "Wrote %s (%d matches) and %s (%d breaks)",
            paths.matches,
            len(result.matches),
            paths.breaks,
            len(result.breaks),
        )

    should_load = load_db if load_db is not None else bool(url)
    db_counts: dict[str, int] | None = None
    if should_load:
        if not url:
            raise ValueError("load_db requested but DATABASE_URL is not set")
        db_counts = load_to_database(result.matches, result.breaks, url)
        logger.info(
            "Loaded into DB: matches=%s breaks=%s",
            db_counts.get("matches"),
            db_counts.get("breaks"),
        )

    return MatchRunResult(
        match_rows=len(result.matches),
        break_rows=len(result.breaks),
        summary=result.summary,
        matches_path=paths.matches,
        breaks_path=paths.breaks,
        db_loaded=bool(db_counts),
        db_counts=db_counts,
        source=source,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Match normalized broker vs desk trades (exact → tolerance → "
            "corporate-action → split-fill). Write Parquet and optionally "
            "load Postgres when DATABASE_URL is set."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=None,
        help="Directory with normalized_trades.parquet",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to write matches.parquet / breaks.parquet",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Market-data cache dir (for splits.parquet)",
    )
    parser.add_argument(
        "--price-tolerance-bps",
        type=float,
        default=DEFAULT_PRICE_TOLERANCE_BPS,
        help=f"Tolerance-pass price band in bps (default {DEFAULT_PRICE_TOLERANCE_BPS:g})",
    )
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="Read normalized trades from RDS instead of Parquet",
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
        result = run_match(
            normalized_dir=args.input_dir,
            output_dir=args.output_dir,
            cache_dir=args.cache_dir,
            write_parquet=not args.no_parquet,
            load_db=load_db,
            from_db=args.from_db,
            price_tolerance_bps=args.price_tolerance_bps,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except (MatchError, ValueError) as exc:
        logger.error("%s", exc)
        return 1

    summary: dict[str, Any] = {
        "matched_at": datetime.now(timezone.utc).isoformat(),
        "source": result.source,
        "match_rows": result.match_rows,
        "break_rows": result.break_rows,
        "match_pass_counts": result.summary.get("match_pass_counts", {}),
        "break_type_counts": result.summary.get("break_type_counts", {}),
        "matches_path": str(result.matches_path),
        "breaks_path": str(result.breaks_path),
        "price_tolerance_bps": args.price_tolerance_bps,
        "db_loaded": result.db_loaded,
        "db_counts": result.db_counts,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

