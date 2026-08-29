"""JSON-only runner, tool-call cap, persist guardrails. No live Bedrock."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from uuid import uuid4

from backend.agent.enums import RootCause, SuggestedAction
from backend.agent.persist import PersistGuardError, persist_suggestion
from backend.agent.providers import MAX_TOOL_CALLS, ProviderTurn, StubProvider, ToolCall
from backend.agent.prompt import build_user_prompt
from backend.agent.runner import (
    default_stub_output,
    investigate_break,
    persist_investigation,
)
from backend.agent.schema import parse_agent_output
from backend.agent.tools import InMemoryStore, ToolContext
from backend.db.models import Break, Match, NormalizedTrade, ResolutionSuggestion


def _break(**overrides: object) -> Break:
    kwargs: dict[str, object] = {
        "break_id": uuid4(),
        "break_type": "quantity_break",
        "status": "open",
        "symbol": "AAPL",
        "trade_date": date(2024, 6, 10),
        "detail": {"broker_qty": 100.0, "desk_qty": 110.0},
    }
    kwargs.update(overrides)
    return Break(**kwargs)  # type: ignore[arg-type]


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(cache_dir=tmp_path, store=InMemoryStore())


def test_user_prompt_keeps_break_payload_when_analyst_notes(tmp_path: Path) -> None:
    brk = _break()
    text = build_user_prompt(
        brk,
        extra_context={"desk": "EQ-US", "notional_at_risk": 1000.0},
        analyst_message="Please check corporate actions.",
    )
    assert str(brk.break_id) in text
    assert "Please check corporate actions." in text
    assert "not a replacement" in text
    assert "EQ-US" in text
    empty = build_user_prompt(brk, analyst_message="")
    assert "break context only" in empty
    assert str(brk.break_id) in empty


def test_json_only_stub_validates_schema(tmp_path: Path) -> None:
    brk = _break()
    provider = StubProvider(default_text=default_stub_output(brk))
    result = investigate_break(
        brk, provider, _ctx(tmp_path), tools_enabled=False
    )
    assert result.output.break_id == brk.break_id
    assert result.output.root_cause == RootCause.QUANTITY_MISMATCH
    assert result.output.suggested_action == SuggestedAction.AMEND_QUANTITY
    assert result.tool_calls == 0
    assert 0.0 <= result.output.confidence <= 1.0


def test_invalid_enum_falls_back_after_retries(tmp_path: Path) -> None:
    brk = _break()
    bad = json.dumps(
        {
            "break_id": str(brk.break_id),
            "root_cause": "made_up",
            "confidence": 0.9,
            "explanation": "nope",
            "suggested_action": "teleport",
            "evidence": [],
        }
    )
    provider = StubProvider(default_text=bad)
    result = investigate_break(
        brk, provider, _ctx(tmp_path), tools_enabled=False
    )
    assert result.output.root_cause == RootCause.INSUFFICIENT_EVIDENCE
    assert result.output.suggested_action == SuggestedAction.ESCALATE_TO_OPS


def test_unknown_root_cause_maps_to_other(tmp_path: Path) -> None:
    brk = _break()
    payload = json.dumps(
        {
            "break_id": str(brk.break_id),
            "root_cause": "made_up",
            "confidence": 0.8,
            "explanation": "Does not fit a named cause.",
            "suggested_action": "escalate_to_ops",
            "evidence": [],
        }
    )
    provider = StubProvider(default_text=payload)
    result = investigate_break(
        brk, provider, _ctx(tmp_path), tools_enabled=False
    )
    assert result.output.root_cause == RootCause.OTHER
    assert result.output.suggested_action == SuggestedAction.ESCALATE_TO_OPS


def test_tool_call_cap_is_five(tmp_path: Path) -> None:
    brk = _break()
    script = [
        ProviderTurn(
            tool_calls=[
                ToolCall(
                    name="get_desk_metadata",
                    input={"desk_code": "EQ-US"},
                    tool_use_id=f"t{i}",
                )
            ]
        )
        for i in range(8)
    ]
    good = default_stub_output(brk)
    provider = StubProvider(script=script, default_text=good)
    result = investigate_break(brk, provider, _ctx(tmp_path), tools_enabled=True)
    assert result.tool_calls == MAX_TOOL_CALLS
    assert result.tool_calls <= 5
    assert result.output.break_id == brk.break_id
    assert all(e.tool == "get_desk_metadata" for e in result.output.evidence)


def test_persist_writes_only_resolution_suggestions(tmp_path: Path) -> None:
    brk = _break()
    provider = StubProvider(default_text=default_stub_output(brk))
    result = investigate_break(
        brk, provider, _ctx(tmp_path), tools_enabled=False
    )

    added: list[object] = []

    class _Session:
        def add(self, obj: object) -> None:
            added.append(obj)

        def flush(self) -> None:
            return None

    persist_investigation(_Session(), result)  # type: ignore[arg-type]
    assert len(added) == 1
    assert isinstance(added[0], ResolutionSuggestion)
    assert not isinstance(added[0], (Break, Match, NormalizedTrade))
    assert added[0].break_id == brk.break_id
    assert added[0].inferred is False


def test_persist_guard_rejects_forbidden_tables() -> None:
    class _Evil:
        __table__ = type("T", (), {"name": "breaks"})()

    from backend.agent.persist import _assert_allowed

    try:
        _assert_allowed(_Evil())
        raise AssertionError("expected PersistGuardError")
    except PersistGuardError:
        pass


def test_parse_roundtrip_does_not_change_contract_keys() -> None:
    brk = _break()
    parsed = parse_agent_output(default_stub_output(brk))
    assert set(parsed.to_contract_dict()) == {
        "break_id",
        "root_cause",
        "confidence",
        "explanation",
        "suggested_action",
        "evidence",
    }
