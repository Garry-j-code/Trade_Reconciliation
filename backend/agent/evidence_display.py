"""Analyst-facing labels for agent evidence (dashboard only).

Stored suggestion JSON keeps tool identifiers (project_plan.md §6.3).
The dashboard maps those names to plain English so internals stay hidden.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# snake_case identifiers the agent persists on evidence[].tool
TOOL_EVIDENCE_LABELS: dict[str, str] = {
    "get_corporate_actions": "Checked corporate actions for this symbol",
    "get_market_session_info": "Looked up the market session for this trade date",
    "get_trade_history": "Reviewed prior trades for this symbol",
    "get_similar_resolved_breaks": (
        "Compared with previously resolved breaks of the same type"
    ),
    "search_similar_breaks": "Searched similar human-resolved cases",
    "get_desk_metadata": "Looked up desk reference data",
    "get_raw_records": "Compared broker and desk source records",
    "get_relevant_memory": "Recalled prior investigation notes",
    "get_trade_pair": "Compared broker and desk prices for this pair",
}

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def evidence_heading(tool: str | None) -> str:
    """Plain-English heading for an evidence item; never a function name."""
    raw = (tool or "").strip()
    if not raw:
        return "Reviewed additional records"
    if raw in TOOL_EVIDENCE_LABELS:
        return TOOL_EVIDENCE_LABELS[raw]
    if " " in raw:
        return raw
    if _IDENT.match(raw):
        return "Reviewed additional records"
    return raw


def evidence_detail(result_summary: str | None, *, tool: str | None = None) -> str:
    """Keep payload facts; strip a leading internal tool name if present."""
    text = (result_summary or "").strip()
    name = (tool or "").strip()
    if name and text.startswith(name):
        rest = text[len(name) :].lstrip(" :.-")
        if rest.lower().startswith("error"):
            msg = rest.split(":", 1)[-1].strip() if ":" in rest else rest
            return f"Could not complete this check: {msg}" if msg else text
        return rest or text
    return text


def display_evidence_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Copy an evidence dict with analyst-facing ``tool`` / ``result_summary``."""
    tool = item.get("tool")
    tool_s = str(tool) if tool is not None else ""
    summary = item.get("result_summary")
    summary_s = str(summary) if summary is not None else ""
    out = dict(item)
    out["tool"] = evidence_heading(tool_s)
    out["result_summary"] = evidence_detail(summary_s, tool=tool_s)
    return out


def display_evidence_list(items: list[Any] | None) -> list[dict[str, Any]]:
    if not items:
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            out.append(display_evidence_item(item))
    return out
