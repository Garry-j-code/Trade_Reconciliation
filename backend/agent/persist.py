"""Persist agent output to ``resolution_suggestions`` only.

Never writes to ``trades`` / ``normalized_trades`` / ``matches`` / ``breaks``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from backend.agent.schema import AgentOutput
from backend.db.models import ResolutionSuggestion

# Tables the agent persist path must never touch.
FORBIDDEN_TABLES = frozenset(
    {
        "normalized_trades",
        "raw_broker_trades",
        "raw_desk_trades",
        "matches",
        "breaks",
        "trades",
    }
)


class PersistGuardError(RuntimeError):
    """Raised if persist is asked to write a forbidden entity."""


def persist_suggestion(
    session: Session,
    output: AgentOutput,
    *,
    inferred: bool = False,
) -> ResolutionSuggestion:
    """Insert one ``resolution_suggestions`` row. Does not mutate the break."""
    row = ResolutionSuggestion(
        break_id=output.break_id if isinstance(output.break_id, UUID) else UUID(str(output.break_id)),
        root_cause=output.root_cause.value,
        confidence=float(output.confidence),
        explanation=output.explanation,
        suggested_action=output.suggested_action.value,
        evidence=output.to_contract_dict()["evidence"],
        inferred=inferred,
    )
    _assert_allowed(row)
    session.add(row)
    session.flush()
    return row


def persist_cluster_copies(
    session: Session,
    representative: AgentOutput,
    sibling_break_ids: list[UUID],
) -> list[ResolutionSuggestion]:
    """Write inferred copies for cluster siblings. Suggestions only."""
    rows: list[ResolutionSuggestion] = []
    for bid in sibling_break_ids:
        copy = representative.model_copy(update={"break_id": bid})
        rows.append(persist_suggestion(session, copy, inferred=True))
    return rows


def _assert_allowed(obj: Any) -> None:
    table = getattr(getattr(obj, "__table__", None), "name", None)
    if table in FORBIDDEN_TABLES:
        raise PersistGuardError(f"Agent must not write to {table}")
    if table != "resolution_suggestions":
        raise PersistGuardError(
            f"Agent persist only writes resolution_suggestions, not {table!r}"
        )
