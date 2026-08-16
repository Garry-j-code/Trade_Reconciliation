"""Investigate open breaks that still lack a resolution_suggestions row.

Called after daily blotter (not from ``backend/pipeline`` matching). One
failure must not abort the rest of the batch. Max 5 tool calls per break.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from backend.agent.cache import cache_dir_from_env, s3_cache_settings
from backend.agent.providers import BedrockAccessError, StubProvider, provider_from_env
from backend.agent.runner import default_stub_output, investigate_break, persist_investigation
from backend.agent.tools import ToolContext, attach_embedder
from backend.db.models import Break, ResolutionSuggestion

logger = logging.getLogger(__name__)


def open_breaks_without_suggestions(
    session: Session, *, limit: int | None = None
) -> list[Break]:
    """Open breaks with no suggestion row yet (backfill + post-blotter)."""
    has_suggestion = exists().where(ResolutionSuggestion.break_id == Break.break_id)
    stmt = (
        select(Break)
        .where(Break.status == "open", ~has_suggestion)
        .order_by(Break.created_at.asc(), Break.break_id.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def count_open_breaks_without_suggestions(session: Session) -> int:
    """How many open breaks still lack a suggestion (cheap; no LLM)."""
    has_suggestion = exists().where(ResolutionSuggestion.break_id == Break.break_id)
    stmt = (
        select(func.count())
        .select_from(Break)
        .where(Break.status == "open", ~has_suggestion)
    )
    return int(session.scalar(stmt) or 0)


def _tool_context(session: Session) -> ToolContext:
    bucket, prefix, region = s3_cache_settings()
    return ToolContext(
        cache_dir=cache_dir_from_env(),
        session=session,
        s3_bucket=bucket,
        s3_prefix=prefix,
        aws_region=region,
    )


def investigate_missing_suggestions(
    session: Session,
    *,
    provider_name: str | None = None,
    limit: int | None = None,
    tools_enabled: bool = True,
) -> dict[str, Any]:
    """Investigate each open break without a suggestion. Continues on errors."""
    breaks = open_breaks_without_suggestions(session, limit=limit)
    summary: dict[str, Any] = {
        "attempted": len(breaks),
        "written": 0,
        "failed": 0,
        "errors": [],
        "suggestion_ids": [],
    }
    if not breaks:
        return summary
    ctx = _tool_context(session)
    try:
        provider = provider_from_env(provider_name)
    except BedrockAccessError as exc:
        logger.warning("Skipping auto-investigate: %s", exc)
        summary["failed"] = len(breaks)
        summary["errors"].append({"break_id": None, "error": str(exc)})
        return summary
    attach_embedder(ctx, provider)

    for brk in breaks:
        try:
            if isinstance(provider, StubProvider) and not provider.script:
                provider.default_text = default_stub_output(brk)
            result = investigate_break(
                brk, provider, ctx, tools_enabled=tools_enabled
            )
            persist_investigation(session, result, inferred=False)
            session.flush()
            summary["written"] += 1
            if result.suggestion_id:
                summary["suggestion_ids"].append(str(result.suggestion_id))
        except Exception as exc:  # noqa: BLE001 — blotter must continue
            logger.exception("Auto-investigate failed for break %s", brk.break_id)
            summary["failed"] += 1
            summary["errors"].append(
                {"break_id": str(brk.break_id), "error": f"{type(exc).__name__}: {exc}"}
            )
    return summary


def break_ids(rows: list[Break]) -> list[UUID]:
    return [row.break_id for row in rows]
