"""Summary, matches list, and local recon trigger."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from time import monotonic
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from backend.api import crud
from backend.api.auth import AuthContext
from backend.api.deps import get_db
from backend.api.models import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from backend.api.schemas import (
    MatchListItem,
    PaginatedMatches,
    ReconRunRequest,
    ReconRunResponse,
    SummaryResponse,
)
from backend.pipeline.normalize import NormalizationError
from backend.pipeline.recon import ReconTimeoutError, run_recon_capped
from backend.pipeline.daily_blotter import run_daily_blotter

router = APIRouter(tags=["recon"])


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
def get_summary(db: Session = Depends(get_db)) -> SummaryResponse:
    stats = crud.summary_stats(db)
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


@router.post("/recon/run", response_model=ReconRunResponse)
def post_recon_run(body: ReconRunRequest | None = None) -> ReconRunResponse:
    """Normalize + match. ``mode=daily`` appends one session; ``mode=full`` is legacy wipe."""
    payload = body or ReconRunRequest()
    input_dir = Path(payload.input_dir) if payload.input_dir else None
    mode = (payload.mode or "daily").strip().lower()
    use_full = mode in {"full", "replace", "all"} or payload.replace
    try:
        if use_full:
            result = run_recon_capped(
                input_dir=input_dir,
                replace=True,
                trade_date=payload.trade_date,
            )
            return ReconRunResponse(
                broker_rows=result.broker_rows,
                desk_rows=result.desk_rows,
                normalized_rows=result.normalized_rows,
                match_count=result.match_count,
                break_count=result.break_count,
                breaks_by_type=result.breaks_by_type,
                elapsed_seconds=result.elapsed_seconds,
                db_loaded=result.db_loaded,
            )
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
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NormalizationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ReconTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc


@router.post("/ops/memory-write")
def post_memory_write() -> dict[str, Any]:
    """Scheduled memory loop. Stub provider by default to cap Bedrock cost."""
    from backend.agent.memory_writer import run_memory_writer
    from backend.agent.providers import StubProvider, stub_embedding
    from backend.db.session import database_url_from_env, get_engine, get_session_factory, session_scope

    url = database_url_from_env()
    if not url:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    engine = get_engine(url)
    factory = get_session_factory(engine)
    provider = StubProvider(default_text='{"notes": []}')
    with session_scope(factory) as session:
        stats = run_memory_writer(
            session,
            provider,
            write_semantic=False,
            embed_fn=stub_embedding,
        )
    return {"ok": True, "provider": "stub", **stats}
