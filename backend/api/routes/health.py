"""Liveness / readiness."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.api import crud
from backend.api.deps import get_db_optional
from backend.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(db: Optional[Session] = Depends(get_db_optional)) -> HealthResponse:
    """Process is up. ``db`` is connected only when RDS ping succeeds."""
    if db is None:
        return HealthResponse(db="unavailable")
    try:
        crud.ping_db(db)
    except Exception:  # noqa: BLE001 — health must not raise on a down DB
        return HealthResponse(db="unavailable")
    return HealthResponse(db="connected")
