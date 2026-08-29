"""System prompt: guardrails + enum contract + concatenated skills."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from backend.agent.enums import ROOT_CAUSE_VALUES, SUGGESTED_ACTION_VALUES
from backend.agent.providers import MAX_TOOL_CALLS
from backend.agent.schema import OUTPUT_JSON_SCHEMA
from backend.agent.skills_loader import investigation_skills_prompt
from backend.db.models import Break
from backend.pipeline.rules import parse_trade_ids

MAX_ANALYST_MESSAGE_CHARS = 4000


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


def build_user_prompt(
    brk: Break,
    *,
    extra_context: dict[str, Any] | None = None,
    analyst_message: str | None = None,
) -> str:
    """Break payload is always included. Analyst text is extra context only."""
    payload = break_to_payload(brk)
    parts = [
        "Investigate this pipeline-computed break and return the JSON output contract.",
        "The explanation field is shown to the analyst in chat as plain language.",
        f"break_id (must be copied exactly): {payload['break_id']}",
        f"break_type: {payload['break_type']}",
        f"symbol: {payload['symbol']}",
        f"trade_date: {payload['trade_date']}",
        f"pair_id: {payload['pair_id']}",
        f"broker_trade_ids: {payload['broker_trade_ids']}",
        f"desk_trade_ids: {payload['desk_trade_ids']}",
        f"detail (pipeline-computed; do not recalculate): {payload['detail']}",
    ]
    if extra_context:
        parts.append(
            "Additional break context already on file (trades, existing suggestion, "
            "evidence display). Use it; do not ask the analyst to paste IDs:\n"
            + json.dumps(extra_context, default=str, indent=2)
        )
    note = (analyst_message or "").strip()
    if len(note) > MAX_ANALYST_MESSAGE_CHARS:
        note = note[:MAX_ANALYST_MESSAGE_CHARS]
    if note:
        parts.append(
            "Analyst note (additional context only — not a replacement for the "
            f"break payload above):\n{note}"
        )
    else:
        parts.append(
            "Analyst note: (none — investigate with the attached break context only)."
        )
    return "\n".join(parts) + "\n"


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
