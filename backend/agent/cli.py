"""CLI: investigate open breaks with the agent (stub or Bedrock).

Usage:
    uv run investigate-breaks
    uv run investigate-breaks --limit 5 --provider stub
    uv run investigate-breaks --break-id <uuid>
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.agent.cache import cache_dir_from_env, s3_cache_settings
from backend.agent.providers import (
    BedrockAccessError,
    StubProvider,
    provider_from_env,
)
from backend.agent.runner import (
    InvestigationResult,
    default_stub_output,
    investigate_break,
    investigate_clusters,
    persist_investigation,
)
from backend.agent.tools import ToolContext
from backend.db.models import Break
from backend.db.session import (
    database_url_from_env,
    get_engine,
    get_session_factory,
    session_scope,
)

logger = logging.getLogger(__name__)


def _tool_context(session: Session) -> ToolContext:
    bucket, prefix, region = s3_cache_settings()
    return ToolContext(
        cache_dir=cache_dir_from_env(),
        session=session,
        s3_bucket=bucket,
        s3_prefix=prefix,
        aws_region=region,
    )


def _load_breaks(session: Session, *, break_id: UUID | None, limit: int | None) -> list[Break]:
    if break_id is not None:
        row = session.get(Break, break_id)
        if row is None:
            raise SystemExit(f"break not found: {break_id}")
        return [row]
    stmt = select(Break).where(Break.status == "open").order_by(Break.created_at.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def _result_json(result: InvestigationResult) -> dict[str, Any]:
    payload = result.output.to_contract_dict()
    payload["inferred"] = result.inferred
    payload["tool_calls"] = result.tool_calls
    payload["review_route"] = result.review_route
    if result.suggestion_id:
        payload["suggestion_id"] = str(result.suggestion_id)
    return payload


def run_investigate(
    session: Session,
    *,
    break_id: UUID | None,
    limit: int | None,
    provider_name: str,
    cluster: bool,
    tools_enabled: bool,
) -> list[dict[str, Any]]:
    breaks = _load_breaks(session, break_id=break_id, limit=limit)
    if not breaks:
        return []
    ctx = _tool_context(session)
    try:
        provider = provider_from_env(provider_name)
    except BedrockAccessError:
        raise
    if isinstance(provider, StubProvider) and not provider.default_text and not provider.script:
        provider = StubProvider(default_factory=lambda **_: default_stub_output(breaks[0]))

    if cluster and break_id is None and len(breaks) > 1:
        results = investigate_clusters(
            session, breaks, provider, ctx, tools_enabled=tools_enabled
        )
    else:
        results = []
        for brk in breaks:
            if isinstance(provider, StubProvider) and not provider.script:
                provider.default_text = default_stub_output(brk)
            result = investigate_break(
                brk, provider, ctx, tools_enabled=tools_enabled
            )
            persist_investigation(session, result, inferred=False)
            results.append(result)
    return [_result_json(r) for r in results]


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Investigate recon breaks with the agent")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--break-id", type=str, default=None)
    parser.add_argument(
        "--provider",
        default=None,
        help="stub | bedrock (default: AGENT_LLM_PROVIDER or bedrock)",
    )
    parser.add_argument("--no-cluster", action="store_true")
    parser.add_argument("--json-only", action="store_true", help="Disable tools")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    url = database_url_from_env()
    if not url:
        print("DATABASE_URL is required to load breaks", file=sys.stderr)
        return 2
    bid = UUID(args.break_id) if args.break_id else None
    engine = get_engine(url)
    factory = get_session_factory(engine)
    try:
        with session_scope(factory) as session:
            rows = run_investigate(
                session,
                break_id=bid,
                limit=args.limit,
                provider_name=args.provider or "bedrock",
                cluster=not args.no_cluster,
                tools_enabled=not args.json_only,
            )
    except BedrockAccessError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(json.dumps({"count": len(rows), "suggestions": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
