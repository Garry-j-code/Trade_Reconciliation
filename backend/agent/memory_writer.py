"""Agent memory: HITL decision rows + cheap nightly backfill.

Primary write is on human Approve/Reject (Titan embed per decision). The
EventBridge 07:00 job backfills any audit rows that missed approve-time write
and skips when caught up. Optional Converse notes stay opt-in (``--semantic``).
Memory is a prior, not a verdict — retrieval must be checked against evidence.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.agent.providers import (
    LLMProvider,
    StubProvider,
    embedder_from_env,
    pad_embedding,
    provider_from_env,
    stub_embedding,
    try_embed,
)
from backend.agent.routing import notional_from_detail
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
HITL_AUDIT_ACTIONS = ("approved", "rejected", "overridden")
DECISION_SCOPE_PREFIX = "decision:"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def notional_band(notional: float | None) -> str:
    """Bucket pipeline notional so memory does not store a precise money figure."""
    if notional is None:
        return "unknown"
    value = abs(float(notional))
    if value < 10_000:
        return "<10k"
    if value < 50_000:
        return "10k-50k"
    if value < 250_000:
        return "50k-250k"
    return "250k+"


def _desks_from_break(brk: Break) -> list[str]:
    detail = brk.detail if isinstance(brk.detail, dict) else {}
    desks: list[str] = []
    desk = detail.get("desk")
    if desk:
        desks.append(str(desk))
    broker_acct = detail.get("broker_account") or detail.get("account")
    if broker_acct and str(broker_acct) not in desks:
        desks.append(str(broker_acct))
    return desks


def _memory_type_for_outcome(action: str) -> str:
    if action == "approved":
        return "incident"
    return "override_reason"


def _decision_scope(audit_id: UUID) -> str:
    return f"{DECISION_SCOPE_PREFIX}{audit_id}"


def decision_facts(
    *,
    brk: Break,
    audit: AuditLog,
    suggestion: ResolutionSuggestion | None,
) -> dict[str, Any]:
    desks = _desks_from_break(brk)
    notional = notional_from_detail(brk.detail if isinstance(brk.detail, dict) else None)
    outcome = str(audit.action)
    root = suggestion.root_cause if suggestion is not None else None
    action = suggestion.suggested_action if suggestion is not None else None
    return {
        "break_type": brk.break_type,
        "desks": desks,
        "symbol": brk.symbol,
        "root_cause": root,
        "suggested_action": action,
        "outcome": outcome,
        "actor_note": audit.override_note,
        "notional_band": notional_band(notional),
        "pair_id": brk.pair_id,
        "audit_id": str(audit.audit_id) if audit.audit_id else None,
        "break_id": str(brk.break_id),
    }


def decision_content(facts: dict[str, Any]) -> str:
    desks = facts.get("desks") or []
    desk_txt = ",".join(str(d) for d in desks) if desks else "unknown"
    note = facts.get("actor_note") or ""
    note_txt = f" Note: {note}" if note else ""
    return (
        f"Human {facts.get('outcome')} {facts.get('break_type')} on "
        f"{facts.get('symbol') or 'UNKNOWN'} desks={desk_txt} "
        f"root_cause={facts.get('root_cause')} "
        f"suggested_action={facts.get('suggested_action')} "
        f"notional_band={facts.get('notional_band')} "
        f"pair_id={facts.get('pair_id')}.{note_txt}"
    )


def _existing_decision_row(session: Session, audit_id: UUID) -> AgentMemory | None:
    return session.scalars(
        select(AgentMemory).where(AgentMemory.audit_id == audit_id).limit(1)
    ).first()


def upsert_decision_memory(
    session: Session,
    *,
    brk: Break,
    audit: AuditLog,
    suggestion: ResolutionSuggestion | None,
    embed_fn: Callable[[str], list[float]] | None = None,
) -> AgentMemory | None:
    """Idempotent HITL memory row. Embedding failure still stores the facts."""
    if audit.audit_id is None:
        session.flush()
    if audit.audit_id is None:
        logger.error("Cannot write agent_memory without audit_id")
        return None
    facts = decision_facts(brk=brk, audit=audit, suggestion=suggestion)
    content = decision_content(facts)
    embed = embed_fn or stub_embedding
    vector = try_embed(embed, content)
    existing = _existing_decision_row(session, audit.audit_id)
    if existing is not None:
        existing.content = content
        existing.facts = facts
        existing.memory_type = _memory_type_for_outcome(str(audit.action))
        existing.scope = _decision_scope(audit.audit_id)
        existing.source_break_ids = [brk.break_id]
        if vector is not None:
            existing.embedding = vector
        session.flush()
        return existing
    row = AgentMemory(
        scope=_decision_scope(audit.audit_id),
        memory_type=_memory_type_for_outcome(str(audit.action)),
        content=content,
        embedding=vector,
        source_break_ids=[brk.break_id],
        audit_id=audit.audit_id,
        facts=facts,
    )
    session.add(row)
    session.flush()
    return row


def record_human_decision_memory(
    session: Session,
    *,
    brk: Break,
    audit: AuditLog,
    suggestion: ResolutionSuggestion | None,
    embed_fn: Callable[[str], list[float]] | None = None,
) -> AgentMemory | None:
    """Called after a successful audit_log write. Never raises to the HITL path."""
    try:
        from backend.agent.providers import embedder_from_env as _embedder_from_env

        fn = embed_fn
        if fn is None:
            fn = _embedder_from_env().embed
        return upsert_decision_memory(
            session, brk=brk, audit=audit, suggestion=suggestion, embed_fn=fn
        )
    except Exception as exc:  # noqa: BLE001 — HITL must still succeed
        logger.exception("agent_memory write failed after audit %s: %s", audit.audit_id, exc)
        return None


def audits_missing_memory(
    session: Session,
    *,
    since: datetime | None = None,
    limit: int = 200,
) -> list[tuple[AuditLog, Break | None, ResolutionSuggestion | None]]:
    """HITL rows that do not yet have an ``agent_memory.audit_id`` match."""
    stmt = (
        select(AuditLog, Break, ResolutionSuggestion)
        .outerjoin(Break, Break.break_id == AuditLog.break_id)
        .outerjoin(
            ResolutionSuggestion,
            ResolutionSuggestion.suggestion_id == AuditLog.suggestion_id,
        )
        .outerjoin(AgentMemory, AgentMemory.audit_id == AuditLog.audit_id)
        .where(AuditLog.action.in_(HITL_AUDIT_ACTIONS))
        .where(AgentMemory.memory_id.is_(None))
        .order_by(AuditLog.created_at.asc())
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(AuditLog.created_at >= since)
    rows: list[tuple[AuditLog, Break | None, ResolutionSuggestion | None]] = []
    for audit, brk, sugg in session.execute(stmt):
        rows.append((audit, brk, sugg))
    return rows


def backfill_decision_memories(
    session: Session,
    *,
    since: datetime | None = None,
    embed_fn: Callable[[str], list[float]] | None = None,
    limit: int = 200,
) -> dict[str, int]:
    """Write Titan (or stub) embeddings for audits not stored at Approve time."""
    missing = audits_missing_memory(session, since=since, limit=limit)
    written = 0
    skipped_no_break = 0
    for audit, brk, sugg in missing:
        if brk is None:
            skipped_no_break += 1
            continue
        upsert_decision_memory(
            session, brk=brk, audit=audit, suggestion=sugg, embed_fn=embed_fn
        )
        written += 1
    return {
        "missing_audits": len(missing),
        "written": written,
        "skipped_no_break": skipped_no_break,
        "caught_up": int(len(missing) == 0),
    }


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
        vector = try_embed(embed, str(note["content"]))
        if vector is None:
            vector = pad_embedding(stub_embedding(str(note["content"])))
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
            select(AgentMemory)
            .where(AgentMemory.created_at < cutoff)
            .where(AgentMemory.audit_id.is_(None))
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
        vector = try_embed(embed, content)
        if vector is None:
            vector = pad_embedding(stub_embedding(content))
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
    write_semantic: bool = False,
    skip_if_caught_up: bool = True,
    write_rollups: bool = False,
) -> dict[str, int]:
    """Backfill missing HITL memories. No nightly Converse unless ``write_semantic``."""
    since = since or (_utcnow() - timedelta(days=1))
    backfill = backfill_decision_memories(session, since=None, embed_fn=embed_fn)
    if skip_if_caught_up and backfill.get("caught_up"):
        return {
            "rollups": 0,
            "semantic": 0,
            "compacted_groups": 0,
            "decision_written": 0,
            "decision_missing": 0,
            "skipped": 1,
            **{f"backfill_{k}": int(v) for k, v in backfill.items()},
        }
    rollup_count = 0
    if write_rollups:
        rollups = deterministic_rollups(session, since=since)
        persist_memory_notes(session, rollups, embed_fn=embed_fn)
        rollup_count = len(rollups)
    semantic_count = 0
    if write_semantic:
        cases = _resolved_since(session, since)
        notes = semantic_notes_from_llm(cases, provider)
        persist_memory_notes(session, notes, embed_fn=embed_fn)
        semantic_count = len(notes)
    compacted = compact_old_notes(session, embed_fn=embed_fn)
    return {
        "rollups": rollup_count,
        "semantic": semantic_count,
        "compacted_groups": compacted,
        "decision_written": int(backfill.get("written", 0)),
        "decision_missing": int(backfill.get("missing_audits", 0)),
        "skipped": 0,
        **{f"backfill_{k}": int(v) for k, v in backfill.items()},
    }


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Backfill agent_memory from audit_log (Titan embed; no Converse by default)"
    )
    parser.add_argument("--provider", default=None, help="stub | bedrock (Converse; only with --semantic)")
    parser.add_argument(
        "--embed-provider",
        default=None,
        help="stub | bedrock (default AGENT_EMBED_PROVIDER or bedrock)",
    )
    parser.add_argument("--since-hours", type=int, default=24)
    parser.add_argument(
        "--semantic",
        action="store_true",
        help="Optional Bedrock Converse memory notes (off by default; costly)",
    )
    parser.add_argument(
        "--no-semantic",
        action="store_true",
        help="Deprecated: semantic notes are already off unless --semantic",
    )
    parser.add_argument(
        "--write-rollups",
        action="store_true",
        help="Also persist deterministic SQL rollup notes",
    )
    parser.add_argument(
        "--no-skip-if-caught-up",
        action="store_true",
        help="Run compaction/rollups even when every audit already has a memory row",
    )
    args = parser.parse_args(argv)

    url = database_url_from_env()
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    engine = get_engine(url)
    factory = get_session_factory(engine)
    llm_name = args.provider
    if args.semantic:
        provider = provider_from_env(llm_name)
    else:
        provider = StubProvider(default_text=json.dumps({"notes": []}))
    if isinstance(provider, StubProvider) and not provider.default_text:
        provider.default_text = json.dumps({"notes": []})
    embed_name = args.embed_provider
    if embed_name is None and llm_name in {"stub", "fake", "mock"}:
        embed_name = "stub"
    embedder = embedder_from_env(embed_name)
    since = _utcnow() - timedelta(hours=args.since_hours)
    with session_scope(factory) as session:
        stats = run_memory_writer(
            session,
            provider,
            since=since,
            embed_fn=embedder.embed,
            write_semantic=bool(args.semantic) and not args.no_semantic,
            skip_if_caught_up=not args.no_skip_if_caught_up,
            write_rollups=args.write_rollups,
        )
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
