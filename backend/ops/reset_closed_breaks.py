"""Remove closed HITL breaks and wipe agent memory for a fresh review start.

Does not touch open breaks, their suggestions, matches, or trades.

Never prints DATABASE_URL.
"""

from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.db.models import AgentMemory, AuditLog, Break, ResolutionSuggestion
from backend.db.session import (
    database_url_from_env,
    ensure_agent_schema_patches,
    get_engine,
    get_session_factory,
    session_scope,
)

logger = logging.getLogger(__name__)

CLOSED_BREAK_STATUSES: tuple[str, ...] = ("resolved", "rejected", "overridden")


def reset_closed_breaks(session: Session) -> dict[str, int]:
    """Idempotent: second run deletes nothing if already reset."""
    memory_deleted = session.execute(delete(AgentMemory)).rowcount or 0

    closed_ids = list(
        session.scalars(
            select(Break.break_id).where(Break.status.in_(CLOSED_BREAK_STATUSES))
        ).all()
    )

    audit_deleted = 0
    suggestions_deleted = 0
    breaks_deleted = 0
    if closed_ids:
        audit_deleted = (
            session.execute(
                delete(AuditLog).where(AuditLog.break_id.in_(closed_ids))
            ).rowcount
            or 0
        )
        suggestions_deleted = (
            session.execute(
                delete(ResolutionSuggestion).where(
                    ResolutionSuggestion.break_id.in_(closed_ids)
                )
            ).rowcount
            or 0
        )
        breaks_deleted = (
            session.execute(
                delete(Break).where(Break.break_id.in_(closed_ids))
            ).rowcount
            or 0
        )

    session.flush()

    open_remaining = session.scalar(
        select(func.count()).select_from(Break).where(Break.status == "open")
    ) or 0
    open_suggestions = session.scalar(
        select(func.count())
        .select_from(ResolutionSuggestion)
        .join(Break, ResolutionSuggestion.break_id == Break.break_id)
        .where(Break.status == "open")
    ) or 0
    memory_remaining = session.scalar(
        select(func.count()).select_from(AgentMemory)
    ) or 0

    return {
        "agent_memory_deleted": int(memory_deleted),
        "closed_breaks_found": len(closed_ids),
        "audit_log_deleted": int(audit_deleted),
        "resolution_suggestions_deleted": int(suggestions_deleted),
        "breaks_deleted": int(breaks_deleted),
        "open_breaks_remaining": int(open_remaining),
        "open_suggestions_remaining": int(open_suggestions),
        "agent_memory_remaining": int(memory_remaining),
    }


def main(argv: list[str] | None = None) -> int:
    del argv
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    url = database_url_from_env()
    if not url:
        logger.error("DATABASE_URL is not set")
        return 1
    engine = get_engine(url)
    ensure_agent_schema_patches(engine)
    factory = get_session_factory(engine)
    with session_scope(factory) as session:
        counts = reset_closed_breaks(session)
    logger.info("Closed-break reset complete: %s", counts)
    print("Closed-break reset complete")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
