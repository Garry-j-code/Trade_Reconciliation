"""Latest resolution suggestion for a break."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.api import crud
from backend.api.deps import get_db
from backend.api.schemas import SuggestionOut
from backend.api.services import suggestion_out

router = APIRouter(tags=["resolutions"])


@router.get("/breaks/{break_id}/suggestion", response_model=SuggestionOut)
def get_suggestion(
    break_id: UUID, db: Session = Depends(get_db)
) -> SuggestionOut:
    row = crud.get_break(db, break_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Break not found")
    sugg = crud.latest_suggestion(db, break_id)
    if sugg is None:
        raise HTTPException(status_code=404, detail="no suggestion for this break")
    return suggestion_out(break_id, sugg)
