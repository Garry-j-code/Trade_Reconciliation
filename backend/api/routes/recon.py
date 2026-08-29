"""Summary, matches list, and local recon trigger."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from backend.api import crud
from backend.api.auth import AuthContext
from backend.api.deps import get_db
from backend.api.models import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from backend.api.schemas import (
    InvestigateStatusResponse,
    MatchListItem,
    PaginatedMatches,
    ReconRunRequest,
    ReconRunResponse,
    SummaryResponse,
)
from backend.pipeline.normalize import NormalizationError
from backend.pipeline.recon import (
    ReconTimeoutError,
    run_recon_capped,
    run_rematch_from_db_capped,
)
from backend.pipeline.daily_blotter import run_daily_blotter

router = APIRouter(tags=["recon"])
logger = logging.getLogger(__name__)

# CloudFront custom-origin read timeout max is 60s. Rematch must finish inside
# that window; Bedrock investigation must not share the request.
API_REMATCH_TIMEOUT_SECONDS = 45.0
_investigate_lock = threading.Lock()
_job_lock = threading.Lock()
_investigate_job: InvestigateJob | None = None


@dataclass
class InvestigateJob:
    job_id: str
    status: str
    attempted: int | None = None
    written: int | None = None
    failed: int | None = None


@dataclass(frozen=True)
class InvestigateScheduleResult:
    status: str
    job_id: str
    attempted: int | None = None
    written: int | None = None
    failed: int | None = None


def reset_investigate_job_state() -> None:
    """Tests only — drop in-memory job state between cases."""
    global _investigate_job
    with _job_lock:
        _investigate_job = None


def _snapshot_job() -> InvestigateJob | None:
    with _job_lock:
        if _investigate_job is None:
            return None
        return InvestigateJob(
            job_id=_investigate_job.job_id,
            status=_investigate_job.status,
            attempted=_investigate_job.attempted,
            written=_investigate_job.written,
            failed=_investigate_job.failed,
        )


def _set_job(job: InvestigateJob) -> None:
    global _investigate_job
    with _job_lock:
        _investigate_job = job


def _update_job(
    job_id: str,
    *,
    status: str | None = None,
    attempted: int | None = None,
    written: int | None = None,
    failed: int | None = None,
) -> None:
    with _job_lock:
        if _investigate_job is None or _investigate_job.job_id != job_id:
            return
        if status is not None:
            _investigate_job.status = status
        if attempted is not None:
            _investigate_job.attempted = attempted
        if written is not None:
            _investigate_job.written = written
        if failed is not None:
            _investigate_job.failed = failed


def _in_flight_schedule() -> InvestigateScheduleResult | None:
    job = _snapshot_job()
    if job is None or job.status not in {"queued", "running"}:
        return None
    return InvestigateScheduleResult(status=job.status, job_id=job.job_id)


def _empty_investigate() -> dict[str, Any]:
    return {"attempted": 0, "written": 0, "failed": 0, "errors": []}


def pending_investigate_count() -> int:
    """Open breaks with no suggestion. Never calls an LLM. 0 in unit tests."""
    from backend.agent.auto_investigate import count_open_breaks_without_suggestions
    from backend.db.session import (
        database_url_from_env,
        get_engine,
        get_session_factory,
        session_scope,
    )

    if os.environ.get("TESTING") == "1":
        return 0
    url = database_url_from_env()
    if not url:
        return 0
    engine = None
    try:
        engine = get_engine(url)
        factory = get_session_factory(engine)
        with session_scope(factory) as session:
            return count_open_breaks_without_suggestions(session)
    except Exception:  # noqa: BLE001 — treat as nothing to queue
        logger.exception("Could not count breaks needing investigation")
        return 0
    finally:
        if engine is not None:
            engine.dispose()


def investigate_after_rematch() -> dict[str, Any]:
    """Agent backfill for open breaks with no suggestion. Never raises to the caller.

    Matching stays in ``backend.pipeline``; this is the same wrapper as
    ``backend.ops.daily_blotter``. One Bedrock failure must not abort rematch.
    """
    from backend.agent.auto_investigate import investigate_missing_suggestions
    from backend.agent.providers import BedrockAccessError
    from backend.db.session import (
        database_url_from_env,
        get_engine,
        get_session_factory,
        session_scope,
    )

    if os.environ.get("TESTING") == "1":
        return _empty_investigate()
    url = database_url_from_env()
    if not url:
        return _empty_investigate()
    engine = None
    try:
        engine = get_engine(url)
        factory = get_session_factory(engine)
        with session_scope(factory) as session:
            return investigate_missing_suggestions(session, provider_name=None)
    except BedrockAccessError as exc:
        logger.warning("Auto-investigate after rematch skipped: %s", exc)
        return {
            "attempted": 0,
            "written": 0,
            "failed": 0,
            "errors": [{"break_id": None, "error": str(exc)}],
        }
    except Exception as exc:  # noqa: BLE001 — rematch already succeeded
        logger.exception("Auto-investigate after rematch failed")
        return {
            "attempted": 0,
            "written": 0,
            "failed": 0,
            "errors": [{"break_id": None, "error": f"{type(exc).__name__}: {exc}"}],
        }
    finally:
        if engine is not None:
            engine.dispose()


def schedule_investigate_after_rematch() -> InvestigateScheduleResult:
    """Run investigation off the HTTP request so CloudFront does not 504.

    Returns ``queued`` when a background job starts, or ``finished`` when
    there is nothing to investigate. Overlapping rematch clicks share one
    in-flight job.
    """
    existing = _in_flight_schedule()
    if existing is not None:
        return existing

    pending = pending_investigate_count()
    if pending == 0:
        job = InvestigateJob(
            job_id=str(uuid4()),
            status="finished",
            attempted=0,
            written=0,
            failed=0,
        )
        existing = _in_flight_schedule()
        if existing is not None:
            return existing
        _set_job(job)
        return InvestigateScheduleResult(
            status="finished",
            job_id=job.job_id,
            attempted=0,
            written=0,
            failed=0,
        )

    job = InvestigateJob(job_id=str(uuid4()), status="queued")
    existing = _in_flight_schedule()
    if existing is not None:
        return existing
    _set_job(job)

    def _run() -> None:
        with _investigate_lock:
            try:
                _update_job(job.job_id, status="running")
                stats = investigate_after_rematch()
                _update_job(
                    job.job_id,
                    status="finished",
                    attempted=int(stats.get("attempted") or 0),
                    written=int(stats.get("written") or 0),
                    failed=int(stats.get("failed") or 0),
                )
                logger.info(
                    "Auto-investigate after rematch finished attempted=%s written=%s failed=%s",
                    stats.get("attempted"),
                    stats.get("written"),
                    stats.get("failed"),
                )
            except Exception:  # noqa: BLE001 — UI must leave the running state
                logger.exception("Auto-investigate after rematch crashed")
                _update_job(
                    job.job_id, status="finished", attempted=0, written=0, failed=0
                )

    threading.Thread(
        target=_run,
        name="investigate-after-rematch",
        daemon=True,
    ).start()
    return InvestigateScheduleResult(status="queued", job_id=job.job_id)


@router.get("/me")
def get_me(request: Request) -> dict[str, Any]:
    """Current analyst identity (Cognito) or local default when auth is off."""
    auth = getattr(request.state, "auth", None)
    if isinstance(auth, AuthContext):
        return {
            "authenticated": True,
            "email": auth.email,
            "username": auth.username,
            "sub": auth.sub,
            "actor": auth.actor,
        }
    return {
        "authenticated": False,
        "email": None,
        "username": None,
        "sub": None,
        "actor": "local-analyst",
    }


@router.get("/summary", response_model=SummaryResponse)
def get_summary(
    db: Session = Depends(get_db),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
) -> SummaryResponse:
    start = from_date or date_from
    end = to_date or date_to
    try:
        start, end = crud.resolve_date_range(from_date=start, to_date=end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stats = crud.summary_stats(db, from_date=start, to_date=end)
    return SummaryResponse.model_validate(stats)


@router.get("/matches", response_model=PaginatedMatches)
def get_matches(
    db: Session = Depends(get_db),
    symbol: str | None = Query(default=None),
    trade_date: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> PaginatedMatches:
    items, total = crud.list_matches(
        db, symbol=symbol, trade_date=trade_date, page=page, page_size=page_size
    )
    return PaginatedMatches(
        items=[
            MatchListItem(
                match_id=m.match_id,
                broker_trade_id=m.broker_trade_id,
                desk_trade_id=m.desk_trade_id,
                pair_id=m.pair_id,
                match_pass=m.match_pass,
                created_at=m.created_at,
            )
            for m in items
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


def _recon_response(
    result: Any,
    *,
    investigate: dict[str, Any] | None = None,
    investigate_status: str | None = None,
    investigate_job_id: str | None = None,
) -> ReconRunResponse:
    stats = investigate or {}
    attempted = stats.get("attempted")
    written = stats.get("written")
    failed = stats.get("failed")
    return ReconRunResponse(
        broker_rows=result.broker_rows,
        desk_rows=result.desk_rows,
        normalized_rows=result.normalized_rows,
        match_count=result.match_count,
        break_count=result.break_count,
        breaks_by_type=result.breaks_by_type,
        elapsed_seconds=result.elapsed_seconds,
        db_loaded=result.db_loaded,
        investigate_status=investigate_status,
        investigate_job_id=investigate_job_id,
        investigate_attempted=int(attempted) if attempted is not None else None,
        investigate_written=int(written) if written is not None else None,
        investigate_failed=int(failed) if failed is not None else None,
    )


@router.post("/recon/run", response_model=ReconRunResponse)
def post_recon_run(body: ReconRunRequest | None = None) -> ReconRunResponse:
    """Rematch the current database book by default.

    Generated Parquet under ``backend/data/generated/`` is a laptop artifact and
    is often absent on EC2. Analyst Run recon therefore rematches
    ``normalized_trades`` already in Postgres.

    ``mode=ingest`` / an explicit ``input_dir`` still normalize from Parquet
    (local pipeline). ``mode=daily`` runs the blotter (CLI / EventBridge).

    Hosted rematch returns as soon as matching finishes. Open breaks without
    suggestions are investigated in a background thread (Bedrock is too slow
    for the CloudFront origin timeout).
    """
    payload = body or ReconRunRequest()
    input_dir = Path(payload.input_dir) if payload.input_dir else None
    mode = (payload.mode or "rematch").strip().lower()
    ingest_modes = {"full", "replace", "all", "ingest", "parquet"}
    daily_modes = {"daily", "blotter"}
    try:
        if input_dir is not None or mode in ingest_modes:
            result = run_recon_capped(
                input_dir=input_dir,
                replace=True,
                trade_date=payload.trade_date,
            )
            return _recon_response(result)
        if mode in daily_modes:
            started = monotonic()
            blotter = run_daily_blotter(
                trade_date=payload.trade_date,
                skip_fetch=True,
                skip_s3_sync=True,
                backfill_sessions=1,
            )
            elapsed = monotonic() - started
            gen = blotter.generate[-1] if blotter.generate else {}
            return ReconRunResponse(
                broker_rows=int(gen.get("n_broker_rows") or 0),
                desk_rows=int(gen.get("n_desk_rows") or 0),
                normalized_rows=int(gen.get("n_broker_rows") or 0)
                + int(gen.get("n_desk_rows") or 0),
                match_count=blotter.match_count,
                break_count=blotter.break_count,
                breaks_by_type={},
                elapsed_seconds=elapsed,
                db_loaded=blotter.db_loaded,
            )
        result = run_rematch_from_db_capped(
            timeout_seconds=API_REMATCH_TIMEOUT_SECONDS
        )
        scheduled = schedule_investigate_after_rematch()
        stats: dict[str, Any] | None = None
        if scheduled.status == "finished":
            stats = {
                "attempted": scheduled.attempted if scheduled.attempted is not None else 0,
                "written": scheduled.written if scheduled.written is not None else 0,
                "failed": scheduled.failed if scheduled.failed is not None else 0,
            }
        return _recon_response(
            result,
            investigate=stats,
            investigate_status=scheduled.status,
            investigate_job_id=scheduled.job_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NormalizationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        message = str(exc)
        status = 503 if "DATABASE_URL" in message else 400
        raise HTTPException(status_code=status, detail=message) from exc
    except ReconTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc


@router.get("/recon/investigate-status", response_model=InvestigateStatusResponse)
def get_investigate_status(
    job_id: str | None = Query(default=None),
) -> InvestigateStatusResponse:
    """Poll the in-memory post-rematch investigation job (no LLM call)."""
    job = _snapshot_job()
    if job is None:
        return InvestigateStatusResponse(job_id=job_id, status="idle")
    return InvestigateStatusResponse(
        job_id=job.job_id,
        status=job.status,
        attempted=job.attempted,
        written=job.written,
        failed=job.failed,
    )


@router.post("/ops/memory-write")
def post_memory_write() -> dict[str, Any]:
    """Backfill HITL memories with Titan embeddings. Skips if already caught up.

    Does not run a nightly Converse job. Approve/Reject already wrote most rows.
    """
    from backend.agent.memory_writer import run_memory_writer
    from backend.agent.providers import StubProvider, embedder_from_env
    from backend.db.session import database_url_from_env, get_engine, get_session_factory, session_scope

    url = database_url_from_env()
    if not url:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    engine = get_engine(url)
    factory = get_session_factory(engine)
    provider = StubProvider(default_text='{"notes": []}')
    embedder = embedder_from_env()
    with session_scope(factory) as session:
        stats = run_memory_writer(
            session,
            provider,
            write_semantic=False,
            skip_if_caught_up=True,
            write_rollups=False,
            embed_fn=embedder.embed,
        )
    return {"ok": True, "provider": "embed-backfill", **stats}
