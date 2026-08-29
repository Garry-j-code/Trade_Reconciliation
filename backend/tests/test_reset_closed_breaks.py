"""Wipe closed breaks + all agent_memory; keep open breaks."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import Session, sessionmaker

if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]
if not hasattr(SQLiteTypeCompiler, "visit_ARRAY"):
    SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "TEXT"  # type: ignore[method-assign]
if not hasattr(SQLiteTypeCompiler, "visit_VECTOR"):
    SQLiteTypeCompiler.visit_VECTOR = lambda self, type_, **kw: "BLOB"  # type: ignore[method-assign]

from backend.db.models import (
    AgentMemory,
    AuditLog,
    Base,
    Break,
    ResolutionSuggestion,
)
from backend.ops.reset_closed_breaks import reset_closed_breaks


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            Break.__table__,
            ResolutionSuggestion.__table__,
            AuditLog.__table__,
            AgentMemory.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, future=True)
    return factory()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _break(status: str) -> Break:
    return Break(
        break_type="price_break",
        status=status,
        pair_id=f"PAIR-{status}-{uuid4().hex[:8]}",
        broker_trade_ids="BRK-1",
        desk_trade_ids="DSK-1",
        symbol="AAPL",
        created_at=_now(),
    )


def test_reset_deletes_closed_breaks_and_all_memory_keeps_open() -> None:
    session = _session()
    open_brk = _break("open")
    resolved = _break("resolved")
    rejected = _break("rejected")
    overridden = _break("overridden")
    session.add_all([open_brk, resolved, rejected, overridden])
    session.flush()

    open_sugg = ResolutionSuggestion(
        break_id=open_brk.break_id,
        root_cause="data_entry_error",
        confidence=0.9,
        explanation="open keep",
        suggested_action="correct_desk_trade",
        created_at=_now(),
    )
    closed_sugg = ResolutionSuggestion(
        break_id=resolved.break_id,
        root_cause="data_entry_error",
        confidence=0.8,
        explanation="closed drop",
        suggested_action="correct_desk_trade",
        created_at=_now(),
    )
    session.add_all([open_sugg, closed_sugg])
    session.flush()

    open_audit = AuditLog(
        break_id=open_brk.break_id,
        suggestion_id=open_sugg.suggestion_id,
        actor="ops",
        action="approved",
        created_at=_now(),
    )
    closed_audit = AuditLog(
        break_id=resolved.break_id,
        suggestion_id=closed_sugg.suggestion_id,
        actor="ops",
        action="approved",
        created_at=_now(),
    )
    session.add_all([open_audit, closed_audit])
    session.flush()

    mem_from_closed = AgentMemory(
        scope="global",
        memory_type="decision",
        content="old HITL",
        audit_id=closed_audit.audit_id,
        created_at=_now(),
    )
    mem_orphan = AgentMemory(
        scope="symbol:AAPL",
        memory_type="pattern",
        content="unrelated note",
        created_at=_now(),
    )
    session.add_all([mem_from_closed, mem_orphan])
    session.commit()

    first = reset_closed_breaks(session)
    session.commit()
    assert first["agent_memory_deleted"] == 2
    assert first["closed_breaks_found"] == 3
    assert first["audit_log_deleted"] == 1
    assert first["resolution_suggestions_deleted"] == 1
    assert first["breaks_deleted"] == 3
    assert first["open_breaks_remaining"] == 1
    assert first["open_suggestions_remaining"] == 1
    assert first["agent_memory_remaining"] == 0

    remaining = list(session.scalars(select(Break)).all())
    assert len(remaining) == 1
    assert remaining[0].status == "open"
    assert session.scalar(select(AuditLog).where(AuditLog.break_id == open_brk.break_id))
    assert session.scalar(
        select(ResolutionSuggestion).where(
            ResolutionSuggestion.break_id == open_brk.break_id
        )
    )

    second = reset_closed_breaks(session)
    session.commit()
    assert second["agent_memory_deleted"] == 0
    assert second["closed_breaks_found"] == 0
    assert second["breaks_deleted"] == 0
    assert second["open_breaks_remaining"] == 1
