"""Automatic memory loop: deterministic rollups + LLM semantic notes.

Scheduled Lambda later (EventBridge). Local: ``uv run write-agent-memory``.
Memory is a prior, not a verdict — retrieval must be checked against evidence.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.agent.providers import (
    EMBEDDING_DIM,
    LLMProvider,
    StubProvider,
    embedder_from_env,
    provider_from_env,
    stub_embedding,
)
from backend.agent.skills_loader import memory_writer_skill_prompt
from backend.db.models import AgentMemory, AuditLog, Break, ResolutionSuggestion
from backend.db.session import (
    database_url_from_env,
    get_engine,
    get_session_factory,
    session_scope,
)

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90
MEMORY_TYPES = ("pattern", "incident", "override_reason")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def deterministic_rollups(
    session: Session,
    *,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Plain SQL aggregates — zero hallucination risk (project_plan.md §6.4)."""
    notes: list[dict[str, Any]] = []
    # Break frequency by symbol + type among resolved/overridden.
    stmt = (
        select(Break.symbol, Break.break_type, func.count())
        .where(Break.status.in_(("resolved", "overridden")))
        .group_by(Break.symbol, Break.break_type)
    )
    if since is not None:
        stmt = stmt.where(Break.created_at >= since)
    for symbol, btype, count in session.execute(stmt):
        notes.append(
            {
                "scope": f"symbol:{symbol or 'UNKNOWN'}",
                "memory_type": "pattern",
                "content": (
                    f"{count} resolved/overridden {btype} break(s) on {symbol}."
                ),
                "source_break_ids": [],
            }
        )

    # Override rate by root cause.
    approved = (
        select(ResolutionSuggestion.root_cause, func.count())
        .join(AuditLog, AuditLog.suggestion_id == ResolutionSuggestion.suggestion_id)
        .where(AuditLog.action == "approved")
        .group_by(ResolutionSuggestion.root_cause)
    )
    overridden = (
        select(ResolutionSuggestion.root_cause, func.count())
        .join(AuditLog, AuditLog.suggestion_id == ResolutionSuggestion.suggestion_id)
        .where(AuditLog.action.in_(("overridden", "rejected")))
        .group_by(ResolutionSuggestion.root_cause)
    )
    appr = {row[0]: int(row[1]) for row in session.execute(approved)}
    over = {row[0]: int(row[1]) for row in session.execute(overridden)}
    for cause in sorted(set(appr) | set(over)):
        a = appr.get(cause, 0)
        o = over.get(cause, 0)
        total = a + o
        rate = (o / total) if total else 0.0
        notes.append(
            {
                "scope": "global",
                "memory_type": "pattern",
                "content": (
                    f"Root cause {cause}: {total} human decisions, "
                    f"override/reject rate {rate:.0%} ({o}/{total})."
                ),
                "source_break_ids": [],
            }
        )
    return notes


def _resolved_since(session: Session, since: datetime) -> list[dict[str, Any]]:
    stmt = (
        select(Break, ResolutionSuggestion, AuditLog)
        .join(
            ResolutionSuggestion,
            ResolutionSuggestion.break_id == Break.break_id,
        )
        .join(AuditLog, AuditLog.break_id == Break.break_id)
        .where(AuditLog.created_at >= since)
        .where(AuditLog.action.in_(("approved", "overridden", "rejected")))
        .order_by(AuditLog.created_at.desc())
        .limit(100)
    )
    rows: list[dict[str, Any]] = []
    for brk, sugg, audit in session.execute(stmt):
        rows.append(
            {
                "break_id": str(brk.break_id),
                "symbol": brk.symbol,
                "break_type": brk.break_type,
                "root_cause": sugg.root_cause,
                "suggested_action": sugg.suggested_action,
                "audit_action": audit.action,
                "override_note": audit.override_note,
            }
        )
    return rows


def _parse_memory_notes(text: str) -> list[dict[str, Any]]:
    from backend.agent.schema import extract_json_object

    try:
        payload = extract_json_object(text)
    except ValueError:
        return []
    notes = payload.get("notes") if isinstance(payload, dict) else None
    if not isinstance(notes, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for raw in notes:
        if not isinstance(raw, dict):
            continue
        mtype = str(raw.get("memory_type") or "pattern")
        if mtype not in MEMORY_TYPES:
            mtype = "pattern"
        content = str(raw.get("content") or "").strip()
        if not content:
            continue
        scope = str(raw.get("scope") or "global")
        ids: list[UUID] = []
        for item in raw.get("source_break_ids") or []:
            try:
                ids.append(UUID(str(item)))
            except (TypeError, ValueError):
                continue
        cleaned.append(
            {
                "scope": scope,
                "memory_type": mtype,
                "content": content,
                "source_break_ids": ids,
            }
        )
    return cleaned


def semantic_notes_from_llm(
    cases: list[dict[str, Any]],
    provider: LLMProvider,
) -> list[dict[str, Any]]:
    if not cases:
        return []
    system = (
        memory_writer_skill_prompt()
        + "\nRespond with ONLY JSON: "
        + '{"notes":[{"scope":"desk:EQ-US|symbol:AAPL|global",'
        + '"memory_type":"pattern|incident|override_reason",'
        + '"content":"...","source_break_ids":["uuid"]}]}'
    )
    user = "Summarize these resolved/overridden breaks into memory notes:\n" + json.dumps(
        cases, default=str
    )[:8000]
    turn = provider.converse(
        messages=[{"role": "user", "content": [{"text": user}]}],
        system=system,
        tools=None,
    )
    return _parse_memory_notes(turn.text)


def persist_memory_notes(
    session: Session,
    notes: list[dict[str, Any]],
    *,
    embed_fn: Any | None = None,
) -> list[AgentMemory]:
    embed = embed_fn or stub_embedding
    rows: list[AgentMemory] = []
    for note in notes:
        vector = embed(str(note["content"]))
        if len(vector) < EMBEDDING_DIM:
            vector = vector + [0.0] * (EMBEDDING_DIM - len(vector))
        else:
            vector = vector[:EMBEDDING_DIM]
        row = AgentMemory(
            scope=str(note["scope"]),
            memory_type=str(note["memory_type"]),
            content=str(note["content"]),
            embedding=vector,
            source_break_ids=note.get("source_break_ids") or None,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def compact_old_notes(
    session: Session,
    *,
    older_than_days: int = RETENTION_DAYS,
    now: datetime | None = None,
    embed_fn: Any | None = None,
) -> int:
    """Compact granular notes older than 90 days into a monthly summary."""
    cutoff = (now or _utcnow()) - timedelta(days=older_than_days)
    old = list(
        session.scalars(
            select(AgentMemory).where(AgentMemory.created_at < cutoff)
        ).all()
    )
    if not old:
        return 0
    groups: dict[tuple[str, str], list[AgentMemory]] = {}
    for row in old:
        month = row.created_at.strftime("%Y-%m") if row.created_at else "unknown"
        groups.setdefault((row.scope, month), []).append(row)
    embed = embed_fn or stub_embedding
    created = 0
    for (scope, month), members in groups.items():
        content = (
            f"Monthly compact {month} for {scope}: {len(members)} note(s). "
            + " | ".join(m.content[:120] for m in members[:5])
        )
        vector = embed(content)
        if len(vector) < EMBEDDING_DIM:
            vector = vector + [0.0] * (EMBEDDING_DIM - len(vector))
        else:
            vector = vector[:EMBEDDING_DIM]
        session.add(
            AgentMemory(
                scope=scope,
                memory_type="pattern",
                content=content,
                embedding=vector,
                source_break_ids=None,
            )
        )
        created += 1
        for member in members:
            session.delete(member)
    session.flush()
    return created


def run_memory_writer(
    session: Session,
    provider: LLMProvider,
    *,
    since: datetime | None = None,
    embed_fn: Any | None = None,
    write_semantic: bool = True,
) -> dict[str, int]:
    since = since or (_utcnow() - timedelta(days=1))
    rollups = deterministic_rollups(session, since=since)
    persist_memory_notes(session, rollups, embed_fn=embed_fn)
    semantic_count = 0
    if write_semantic:
        cases = _resolved_since(session, since)
        notes = semantic_notes_from_llm(cases, provider)
        persist_memory_notes(session, notes, embed_fn=embed_fn)
        semantic_count = len(notes)
    compacted = compact_old_notes(session, embed_fn=embed_fn)
    return {
        "rollups": len(rollups),
        "semantic": semantic_count,
        "compacted_groups": compacted,
    }


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Write agent_memory rollups + notes")
    parser.add_argument("--provider", default=None, help="stub | bedrock")
    parser.add_argument("--since-hours", type=int, default=24)
    parser.add_argument("--no-semantic", action="store_true")
    args = parser.parse_args(argv)

    url = database_url_from_env()
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    engine = get_engine(url)
    factory = get_session_factory(engine)
    provider = provider_from_env(args.provider)
    if isinstance(provider, StubProvider) and not provider.default_text:
        provider.default_text = json.dumps({"notes": []})
    embedder = embedder_from_env(
        "stub" if args.provider in {None, "stub"} and isinstance(provider, StubProvider) else None
    )
    since = _utcnow() - timedelta(hours=args.since_hours)
    with session_scope(factory) as session:
        stats = run_memory_writer(
            session,
            provider,
            since=since,
            embed_fn=embedder.embed,
            write_semantic=not args.no_semantic,
        )
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
