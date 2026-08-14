"""Investigate a break: JSON-only first, then optional tool loop (cap 5).

Writes only to ``resolution_suggestions``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from backend.agent.clustering import (
    apply_output_across_cluster,
    cluster_breaks,
    stamp_cluster_ids,
)
from backend.agent.persist import persist_cluster_copies, persist_suggestion
from backend.agent.prompt import (
    break_to_payload,
    build_system_prompt,
    build_user_prompt,
    fallback_output,
)
from backend.agent.providers import (
    MAX_TOOL_CALLS,
    LLMProvider,
    ProviderTurn,
    StubProvider,
    assistant_message_from_turn,
    make_tool_result_message,
)
from backend.agent.routing import notional_from_detail, review_routing
from backend.agent.schema import AgentOutput, EvidenceItem, parse_agent_output
from backend.agent.tools import (
    ToolContext,
    bedrock_tool_specs,
    dispatch_tool,
    summarize_tool_result,
)
from backend.db.models import Break

logger = logging.getLogger(__name__)

MAX_ROUNDS = MAX_TOOL_CALLS + 2


@dataclass
class ToolLogEntry:
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    summary: str


@dataclass
class InvestigationResult:
    output: AgentOutput
    inferred: bool
    tool_calls: int
    tool_log: list[ToolLogEntry] = field(default_factory=list)
    suggestion_id: UUID | None = None
    review_route: str = "manual_review"
    raw_text: str = ""


def _bind_evidence(output: AgentOutput, tool_log: list[ToolLogEntry]) -> AgentOutput:
    """Evidence must trace to tools actually called."""
    if not tool_log:
        return output
    evidence = [
        EvidenceItem(tool=entry.name, result_summary=entry.summary) for entry in tool_log
    ]
    return output.model_copy(update={"evidence": evidence})


def _force_break_id(output: AgentOutput, break_id: UUID) -> AgentOutput:
    if output.break_id != break_id:
        return output.model_copy(update={"break_id": break_id})
    return output


def default_stub_output(brk: Break) -> str:
    """Deterministic JSON when using StubProvider without a script."""
    mapping = {
        "missing_broker": ("missing_trade", "book_missing_trade"),
        "missing_desk": ("missing_trade", "book_missing_trade"),
        "quantity_break": ("quantity_mismatch", "amend_quantity"),
        "price_break": ("price_mismatch", "amend_price"),
        "duplicate": ("duplicate_booking", "cancel_duplicate"),
        "settlement_date_mismatch": (
            "settlement_date_mismatch",
            "amend_settlement_date",
        ),
        "split_fill": ("split_fill", "accept_broker"),
    }
    root, action = mapping.get(brk.break_type, ("insufficient_evidence", "escalate_to_ops"))
    payload = {
        "break_id": str(brk.break_id),
        "root_cause": root,
        "confidence": 0.4,
        "explanation": (
            f"Stub investigation for {brk.break_type} on {brk.symbol}. "
            "Pipeline fields were accepted as given; no live model was called. "
            f"Proposed action is {action}."
        ),
        "suggested_action": action,
        "evidence": [],
    }
    import json

    return json.dumps(payload)


def investigate_break(
    brk: Break,
    provider: LLMProvider,
    ctx: ToolContext,
    *,
    tools_enabled: bool = True,
    skills_dir: Path | None = None,
    max_tool_calls: int = MAX_TOOL_CALLS,
) -> InvestigationResult:
    """Run the agent for one break. Does not persist."""
    system = build_system_prompt(skills_dir=skills_dir)
    user = build_user_prompt(brk)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"text": user}]}
    ]
    tools = bedrock_tool_specs() if tools_enabled else None
    tool_log: list[ToolLogEntry] = []
    last_text = ""

    for _round in range(MAX_ROUNDS):
        if len(tool_log) >= max_tool_calls and tools_enabled:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "text": (
                                f"Tool-call budget of {max_tool_calls} is exhausted. "
                                "Do not call more tools. Output the JSON contract now."
                            )
                        }
                    ],
                }
            )
            tools = None

        turn: ProviderTurn = provider.converse(
            messages=messages, system=system, tools=tools
        )
        last_text = turn.text or last_text

        if turn.tool_calls and tools is not None:
            remaining = max(0, max_tool_calls - len(tool_log))
            results: list[dict[str, Any]] = []
            for index, call in enumerate(turn.tool_calls):
                if index < remaining:
                    result = dispatch_tool(call.name, call.input, ctx)
                    tool_log.append(
                        ToolLogEntry(
                            name=call.name,
                            arguments=dict(call.input),
                            result=result,
                            summary=summarize_tool_result(call.name, result),
                        )
                    )
                else:
                    result = {
                        "error": (
                            f"Tool-call cap of {max_tool_calls} reached; "
                            f"{call.name} was not executed"
                        )
                    }
                results.append(result)
            messages.append(assistant_message_from_turn(turn))
            messages.append(make_tool_result_message(turn.tool_calls, results))
            continue

        if turn.text:
            try:
                output = parse_agent_output(turn.text)
                output = _force_break_id(output, brk.break_id)
                output = _bind_evidence(output, tool_log)
                return InvestigationResult(
                    output=output,
                    inferred=False,
                    tool_calls=len(tool_log),
                    tool_log=tool_log,
                    review_route=review_routing(
                        output.confidence, notional_from_detail(brk.detail)
                    ),
                    raw_text=turn.text,
                )
            except (ValueError, Exception):
                logger.info("Model text was not valid AgentOutput; continuing")
                messages.append(assistant_message_from_turn(turn))
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": (
                                    "Your last message was not valid output-contract JSON "
                                    "with pinned enums. Reply with ONLY the JSON object."
                                )
                            }
                        ],
                    }
                )
                continue

    explanation = (
        "The agent hit the tool-call or turn cap without a valid JSON suggestion. "
        "Escalate for manual review."
    )
    output = parse_agent_output(fallback_output(brk.break_id, explanation))
    output = _bind_evidence(output, tool_log)
    return InvestigationResult(
        output=output,
        inferred=False,
        tool_calls=len(tool_log),
        tool_log=tool_log,
        review_route="manual_review",
        raw_text=last_text,
    )


def persist_investigation(
    session: Session,
    result: InvestigationResult,
    *,
    inferred: bool | None = None,
) -> InvestigationResult:
    flag = result.inferred if inferred is None else inferred
    row = persist_suggestion(session, result.output, inferred=flag)
    result.suggestion_id = row.suggestion_id
    result.inferred = flag
    return result


def investigate_and_persist(
    session: Session,
    brk: Break,
    provider: LLMProvider,
    ctx: ToolContext,
    *,
    tools_enabled: bool = True,
    inferred: bool = False,
) -> InvestigationResult:
    result = investigate_break(
        brk, provider, ctx, tools_enabled=tools_enabled
    )
    return persist_investigation(session, result, inferred=inferred)


def investigate_clusters(
    session: Session,
    breaks: list[Break],
    provider: LLMProvider,
    ctx: ToolContext,
    *,
    tools_enabled: bool = True,
    stamp_ids: bool = True,
) -> list[InvestigationResult]:
    """Investigate one representative per cluster; copy with inferred=True."""
    clusters = cluster_breaks(breaks)
    if stamp_ids:
        stamp_cluster_ids(session, clusters)
    results: list[InvestigationResult] = []
    by_id = {b.break_id: b for b in breaks}
    for cluster in clusters:
        rep = cluster.representative
        if not isinstance(rep, Break):
            rep = by_id[_as_uuid(rep)]
        result = investigate_break(rep, provider, ctx, tools_enabled=tools_enabled)
        persist_investigation(session, result, inferred=False)
        results.append(result)
        if cluster.sibling_ids:
            persist_cluster_copies(session, result.output, cluster.sibling_ids)
            copies = apply_output_across_cluster(
                result.output.to_contract_dict(), cluster
            )
            for payload in copies:
                if payload["inferred"]:
                    sibling = by_id[UUID(payload["break_id"])]
                    sibling_out = parse_agent_output(payload)
                    results.append(
                        InvestigationResult(
                            output=sibling_out,
                            inferred=True,
                            tool_calls=0,
                            review_route=review_routing(
                                sibling_out.confidence,
                                notional_from_detail(sibling.detail),
                            ),
                        )
                    )
    return results


def _as_uuid(brk: Break | dict[str, Any]) -> UUID:
    if isinstance(brk, Break):
        return brk.break_id
    raw = brk.get("break_id")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


def stub_provider_for_break(brk: Break) -> StubProvider:
    return StubProvider(default_text=default_stub_output(brk))


def break_payload(brk: Break) -> dict[str, Any]:
    return break_to_payload(brk)
