"""Recon orchestration: rematch the Postgres book, or normalize Parquet locally.

``run_rematch_from_db`` is the hosted analyst path (no generated Parquet).
``run_recon`` still normalizes laptop Parquet then matches. No LLM calls.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from backend.db.session import database_url_from_env
from backend.pipeline.ingest import run_normalize
from backend.pipeline.matcher import (
    break_row_to_orm,
    match_row_to_orm,
    read_normalized_from_db,
    run_match,
)

DEFAULT_RECON_TIMEOUT_SECONDS = 120.0


class ReconTimeoutError(TimeoutError):
    """Raised when a recon run exceeds the configured wall-clock cap."""


def recon_timeout_seconds(env: dict[str, str] | None = None) -> float:
    """``RECON_TIMEOUT_SECONDS`` or the default cap."""
    source = env if env is not None else os.environ
    raw = (source.get("RECON_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_RECON_TIMEOUT_SECONDS
    return max(1.0, float(raw))


def matches_to_orm(matches: Any) -> list[Any]:
    """Convert a matcher matches frame to ORM rows."""
    if matches is None or getattr(matches, "empty", True):
        return []
    return [match_row_to_orm(r) for r in matches.to_dict(orient="records")]


def breaks_to_orm(breaks: Any) -> list[Any]:
    """Convert a matcher breaks frame to ORM rows."""
    if breaks is None or getattr(breaks, "empty", True):
        return []
    return [break_row_to_orm(r) for r in breaks.to_dict(orient="records")]


@dataclass
class ReconRunResult:
    broker_rows: int
    desk_rows: int
    normalized_rows: int
    match_count: int
    break_count: int
    breaks_by_type: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    db_loaded: bool = False
    parquet_path: Path | None = None
    matches_path: Path | None = None
    breaks_path: Path | None = None


def run_recon(
    *,
    input_dir: Path | None = None,
    output_dir: Path | None = None,
    cache_dir: Path | None = None,
    matched_output_dir: Path | None = None,
    database_url: str | None = None,
    write_parquet: bool = True,
    load_db: bool | None = None,
    replace: bool = True,
    trade_date: date | None = None,
) -> ReconRunResult:
    """Normalize generated trades, match, optionally persist to Postgres.

    When ``trade_date`` is set, raw/normalized tables replace that session only
    and the matcher rematches the full book without deleting ``audit_log``.
    """
    started = datetime.now(timezone.utc)
    url = database_url if database_url is not None else database_url_from_env()
    should_load = load_db if load_db is not None else bool(url)

    norm = run_normalize(
        input_dir=input_dir,
        output_dir=output_dir,
        database_url=url,
        write_parquet=write_parquet,
        load_db=should_load,
        trade_date=trade_date,
        replace=replace if trade_date is None else False,
    )
    match = run_match(
        normalized_dir=norm.parquet_path.parent if write_parquet else output_dir,
        output_dir=matched_output_dir,
        cache_dir=cache_dir,
        database_url=url,
        write_parquet=write_parquet,
        load_db=should_load,
        from_db=should_load and not write_parquet,
    )

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    breaks_by_type = {
        str(k): int(v)
        for k, v in (match.summary.get("break_type_counts") or {}).items()
    }
    return ReconRunResult(
        broker_rows=norm.broker_rows,
        desk_rows=norm.desk_rows,
        normalized_rows=norm.normalized_rows,
        match_count=match.match_rows,
        break_count=match.break_rows,
        breaks_by_type=breaks_by_type,
        elapsed_seconds=elapsed,
        db_loaded=bool(norm.db_loaded or match.db_loaded),
        parquet_path=norm.parquet_path,
        matches_path=match.matches_path,
        breaks_path=match.breaks_path,
    )


def run_rematch_from_db(
    *,
    database_url: str | None = None,
    cache_dir: Path | None = None,
) -> ReconRunResult:
    """Rematch the current normalized book in Postgres. No generated Parquet required.

    Analyst ``POST /api/recon/run`` uses this path. Generation / daily blotter
    stay on EventBridge and the CLI.
    """
    started = datetime.now(timezone.utc)
    url = database_url if database_url is not None else database_url_from_env()
    if not url:
        raise ValueError("DATABASE_URL is not configured")
    normalized = read_normalized_from_db(url)
    if normalized is None or getattr(normalized, "empty", True):
        raise ValueError(
            "No normalized trades in the database. "
            "Run the daily blotter (CLI / EventBridge) before rematching."
        )
    source = normalized["source"] if "source" in normalized.columns else None
    broker_rows = int((source == "broker").sum()) if source is not None else 0
    desk_rows = int((source == "desk").sum()) if source is not None else 0

    match = run_match(
        cache_dir=cache_dir,
        database_url=url,
        write_parquet=False,
        load_db=True,
        from_db=True,
    )
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    breaks_by_type = {
        str(k): int(v)
        for k, v in (match.summary.get("break_type_counts") or {}).items()
    }
    return ReconRunResult(
        broker_rows=broker_rows,
        desk_rows=desk_rows,
        normalized_rows=int(len(normalized)),
        match_count=match.match_rows,
        break_count=match.break_rows,
        breaks_by_type=breaks_by_type,
        elapsed_seconds=elapsed,
        db_loaded=bool(match.db_loaded),
    )


def _run_capped(
    fn: Any,
    *,
    timeout_seconds: float | None = None,
    **kwargs: Any,
) -> ReconRunResult:
    cap = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else recon_timeout_seconds()
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn, **kwargs)
        try:
            return future.result(timeout=cap)
        except FuturesTimeout as exc:
            raise ReconTimeoutError(f"Recon run exceeded {cap:.0f}s cap") from exc


def run_recon_capped(
    *,
    timeout_seconds: float | None = None,
    **kwargs: Any,
) -> ReconRunResult:
    """Run ``run_recon`` in a worker thread; raise if the wall-clock cap is hit."""
    return _run_capped(run_recon, timeout_seconds=timeout_seconds, **kwargs)


def run_rematch_from_db_capped(
    *,
    timeout_seconds: float | None = None,
    **kwargs: Any,
) -> ReconRunResult:
    """Wall-clock cap around ``run_rematch_from_db``."""
    return _run_capped(
        run_rematch_from_db, timeout_seconds=timeout_seconds, **kwargs
    )
