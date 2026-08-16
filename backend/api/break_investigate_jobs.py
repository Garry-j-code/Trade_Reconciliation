"""Per-break Bedrock investigate jobs. HTTP returns immediately (CloudFront 60s)."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from backend.api.schemas import AgentSuggestionOut, EvidenceOut

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_jobs: dict[str, BreakInvestigateJob] = {}
_in_flight_by_break: dict[UUID, str] = {}


@dataclass
class BreakInvestigateJob:
    job_id: str
    break_id: UUID
    status: str
    message: str = ""
    provider: str | None = None
    tools_enabled: bool = True
    reply: str | None = None
    error: str | None = None
    suggestion: AgentSuggestionOut | None = None


def reset_break_investigate_jobs() -> None:
    """Tests only."""
    with _lock:
        _jobs.clear()
        _in_flight_by_break.clear()


def _snapshot(job: BreakInvestigateJob) -> BreakInvestigateJob:
    return BreakInvestigateJob(
        job_id=job.job_id,
        break_id=job.break_id,
        status=job.status,
        message=job.message,
        provider=job.provider,
        tools_enabled=job.tools_enabled,
        reply=job.reply,
        error=job.error,
        suggestion=job.suggestion,
    )


def get_job(job_id: str) -> BreakInvestigateJob | None:
    with _lock:
        job = _jobs.get(job_id)
        return _snapshot(job) if job is not None else None


def _in_flight_for_break(break_id: UUID) -> BreakInvestigateJob | None:
    with _lock:
        job_id = _in_flight_by_break.get(break_id)
        if not job_id:
            return None
        job = _jobs.get(job_id)
        if job is None or job.status not in {"queued", "running"}:
            return None
        return _snapshot(job)


def _plain_error(text: str) -> str:
    lowered = text.lower()
    if "<html" in lowered or "<!doctype" in lowered or "cloudfront" in lowered:
        return "Investigation failed. Try again in a moment."
    cleaned = " ".join(text.split())
    if len(cleaned) > 280:
        return cleaned[:277] + "..."
    return cleaned or "Investigation failed."


def suggestion_from_result(result: Any) -> AgentSuggestionOut:
    contract = result.output.to_contract_dict()
    evidence = [
        EvidenceOut(tool=str(item.get("tool", "")), result_summary=str(item.get("result_summary", "")))
        if isinstance(item, dict)
        else EvidenceOut(tool=item.tool, result_summary=item.result_summary)
        for item in contract["evidence"]
    ]
    return AgentSuggestionOut(
        break_id=result.output.break_id,
        root_cause=result.output.root_cause,
        confidence=result.output.confidence,
        explanation=result.output.explanation,
        suggested_action=result.output.suggested_action,
        evidence=evidence,
        inferred=result.inferred,
        tool_calls=result.tool_calls,
        review_route=result.review_route,
        suggestion_id=result.suggestion_id,
    )


def execute_investigation(
    session: Session,
    *,
    brk: Any,
    message: str,
    provider_name: str | None,
    tools_enabled: bool,
) -> AgentSuggestionOut:
    """Run the existing Converse path and persist a suggestion. No SQL tool."""
    from backend.agent.cache import cache_dir_from_env, s3_cache_settings
    from backend.agent.providers import StubProvider, provider_from_env
    from backend.agent.runner import default_stub_output, investigate_break, persist_investigation
    from backend.agent.tools import ToolContext, attach_embedder
    from backend.api.services import assemble_investigate_context

    extra = assemble_investigate_context(session, brk)
    bucket, prefix, region = s3_cache_settings()
    ctx = ToolContext(
        cache_dir=cache_dir_from_env(),
        session=session,
        s3_bucket=bucket,
        s3_prefix=prefix,
        aws_region=region,
    )
    provider = provider_from_env(provider_name)
    attach_embedder(ctx, provider)
    if isinstance(provider, StubProvider) and not provider.default_text:
        provider.default_text = default_stub_output(brk)
    result = investigate_break(
        brk,
        provider,
        ctx,
        tools_enabled=tools_enabled,
        extra_context=extra,
        analyst_message=message,
    )
    persist_investigation(session, result, inferred=False)
    return suggestion_from_result(result)


def _execute_fresh(job: BreakInvestigateJob) -> None:
    from backend.db.session import (
        database_url_from_env,
        get_engine,
        get_session_factory,
        session_scope,
    )
    from backend.api import crud

    url = database_url_from_env()
    if not url:
        _finish_error(job.job_id, "DATABASE_URL is not configured")
        return
    engine = None
    try:
        engine = get_engine(url)
        factory = get_session_factory(engine)
        with session_scope(factory) as session:
            brk = crud.get_break(session, job.break_id)
            if brk is None:
                _finish_error(job.job_id, "Break not found")
                return
            suggestion = execute_investigation(
                session,
                brk=brk,
                message=job.message,
                provider_name=job.provider,
                tools_enabled=job.tools_enabled,
            )
            _finish_ok(job.job_id, suggestion)
    except Exception as exc:  # noqa: BLE001 — UI must leave running state
        logger.exception("Break investigate job %s failed", job.job_id)
        from backend.agent.providers import BedrockAccessError

        if isinstance(exc, BedrockAccessError):
            _finish_error(job.job_id, _plain_error(str(exc)))
        else:
            _finish_error(job.job_id, _plain_error(f"{type(exc).__name__}: {exc}"))
    finally:
        if engine is not None:
            engine.dispose()


def _finish_ok(job_id: str, suggestion: AgentSuggestionOut) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.status = "finished"
        job.suggestion = suggestion
        job.reply = suggestion.explanation
        job.error = None
        _in_flight_by_break.pop(job.break_id, None)


def _finish_error(job_id: str, error: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.status = "error"
        job.error = error
        job.reply = None
        _in_flight_by_break.pop(job.break_id, None)


def _mark_running(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.status = "running"


def schedule_break_investigate(
    *,
    brk: Any,
    message: str,
    provider: str | None,
    tools_enabled: bool,
    session: Session,
) -> BreakInvestigateJob:
    """Queue (or share) a per-break job. TESTING runs inline on ``session``."""
    existing = _in_flight_for_break(brk.break_id)
    if existing is not None:
        return existing

    job = BreakInvestigateJob(
        job_id=str(uuid4()),
        break_id=brk.break_id,
        status="queued",
        message=message,
        provider=provider,
        tools_enabled=tools_enabled,
    )
    with _lock:
        again = _in_flight_by_break.get(brk.break_id)
        if again:
            current = _jobs.get(again)
            if current is not None and current.status in {"queued", "running"}:
                return _snapshot(current)
        _jobs[job.job_id] = job
        _in_flight_by_break[brk.break_id] = job.job_id

    def _run() -> None:
        _mark_running(job.job_id)
        try:
            if os.environ.get("TESTING") == "1":
                suggestion = execute_investigation(
                    session,
                    brk=brk,
                    message=message,
                    provider_name=provider,
                    tools_enabled=tools_enabled,
                )
                _finish_ok(job.job_id, suggestion)
            else:
                _execute_fresh(job)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Break investigate job %s crashed", job.job_id)
            from backend.agent.providers import BedrockAccessError

            if isinstance(exc, BedrockAccessError):
                _finish_error(job.job_id, _plain_error(str(exc)))
            else:
                _finish_error(job.job_id, _plain_error(f"{type(exc).__name__}: {exc}"))

    if os.environ.get("TESTING") == "1":
        _run()
    else:
        threading.Thread(
            target=_run,
            name=f"investigate-break-{job.job_id[:8]}",
            daemon=True,
        ).start()
    return get_job(job.job_id) or job
