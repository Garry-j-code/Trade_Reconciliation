"""System prompt: guardrails + enum contract + concatenated skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from backend.agent.enums import ROOT_CAUSE_VALUES, SUGGESTED_ACTION_VALUES
from backend.agent.providers import MAX_TOOL_CALLS
from backend.agent.schema import OUTPUT_JSON_SCHEMA
from backend.agent.skills_loader import investigation_skills_prompt
from backend.db.models import Break
from backend.pipeline.rules import parse_trade_ids


def build_system_prompt(*, skills_dir: Path | None = None) -> str:
    skills = investigation_skills_prompt(skills_dir=skills_dir)
    return f"""You are the judgment layer of a trade-reconciliation platform.

Hard rules:
- The deterministic pipeline already decided *that* this is a break. You explain *why* and propose an action.
- Never do arithmetic reconciliation on money (no new notionals, P&L, or price/qty math). Reason over pipeline-computed fields only.
- Never write to trades, matches, or breaks. You only produce the JSON suggestion.
- root_cause and suggested_action must be enum values from the lists below — never free text.
- At most {MAX_TOOL_CALLS} tool calls. After that, output JSON immediately.
- Memory and similar-break tools (`search_similar_breaks`, `get_relevant_memory`)
  are priors / hypotheses, not verdicts. If you adopt a prior, use its enum
  values — never free-text causes or actions.
- evidence[] must refer to tools you actually called.

Allowed root_cause values:
{', '.join(ROOT_CAUSE_VALUES)}

Allowed suggested_action values:
{', '.join(SUGGESTED_ACTION_VALUES)}

After you finish investigating, respond with ONLY a JSON object matching this schema
(no markdown, no preamble):
{OUTPUT_JSON_SCHEMA}

---

{skills}
"""


def break_to_payload(brk: Break) -> dict[str, Any]:
    return {
        "break_id": str(brk.break_id),
        "break_type": brk.break_type,
        "status": brk.status,
        "pair_id": brk.pair_id,
        "symbol": brk.symbol,
        "trade_date": brk.trade_date.isoformat() if brk.trade_date else None,
        "broker_trade_ids": parse_trade_ids(brk.broker_trade_ids),
        "desk_trade_ids": parse_trade_ids(brk.desk_trade_ids),
        "detail": brk.detail or {},
        "cluster_id": str(brk.cluster_id) if brk.cluster_id else None,
    }


def build_user_prompt(brk: Break) -> str:
    payload = break_to_payload(brk)
    return (
        "Investigate this pipeline-computed break and return the JSON output contract.\n"
        f"break_id (must be copied exactly): {payload['break_id']}\n"
        f"break_type: {payload['break_type']}\n"
        f"symbol: {payload['symbol']}\n"
        f"trade_date: {payload['trade_date']}\n"
        f"pair_id: {payload['pair_id']}\n"
        f"broker_trade_ids: {payload['broker_trade_ids']}\n"
        f"desk_trade_ids: {payload['desk_trade_ids']}\n"
        f"detail (pipeline-computed; do not recalculate): {payload['detail']}\n"
    )


def fallback_output(break_id: UUID, explanation: str) -> dict[str, Any]:
    """Used when the model exhausts tools / returns invalid JSON."""
    return {
        "break_id": str(break_id),
        "root_cause": "insufficient_evidence",
        "confidence": 0.1,
        "explanation": explanation,
        "suggested_action": "escalate_to_ops",
        "evidence": [],
    }
