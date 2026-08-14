"""Agent enums, §6.3 output contract, and skill files."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.agent.enums import (
    ROOT_CAUSE_VALUES,
    SUGGESTED_ACTION_VALUES,
    RootCause,
    SuggestedAction,
    parse_root_cause,
    parse_suggested_action,
)
from backend.agent.schema import (
    OUTPUT_JSON_SCHEMA,
    parse_agent_output,
)
from backend.agent.skills_loader import (
    INVESTIGATION_SKILLS,
    MEMORY_WRITER_SKILL,
    concatenate_skills,
    investigation_skills_prompt,
    load_skills,
    parse_frontmatter,
    skill_inventory,
)


def test_root_cause_enum_is_closed() -> None:
    assert "insufficient_evidence" in ROOT_CAUSE_VALUES
    assert "corporate_action_timing" in ROOT_CAUSE_VALUES
    parse_root_cause("price_mismatch")
    with pytest.raises(ValueError, match="Unknown root_cause"):
        parse_root_cause("fat_finger")


def test_suggested_action_enum_is_closed() -> None:
    assert "escalate_to_ops" in SUGGESTED_ACTION_VALUES
    parse_suggested_action("amend_quantity")
    with pytest.raises(ValueError, match="Unknown suggested_action"):
        parse_suggested_action("just_fix_it")


def _valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "break_id": str(uuid4()),
        "root_cause": RootCause.QUANTITY_MISMATCH.value,
        "confidence": 0.7,
        "explanation": "Qty differs and no split is in the cache window.",
        "suggested_action": SuggestedAction.AMEND_QUANTITY.value,
        "evidence": [{"tool": "get_corporate_actions", "result_summary": "no splits"}],
    }
    payload.update(overrides)
    return payload


def test_parse_agent_output_accepts_contract() -> None:
    parsed = parse_agent_output(_valid_payload())
    dumped = parsed.to_contract_dict()
    assert set(dumped) == {
        "break_id",
        "root_cause",
        "confidence",
        "explanation",
        "suggested_action",
        "evidence",
    }
    assert dumped["evidence"][0]["tool"] == "get_corporate_actions"


def test_parse_agent_output_rejects_unknown_enum() -> None:
    with pytest.raises(ValidationError):
        parse_agent_output(_valid_payload(root_cause="not_a_cause"))
    with pytest.raises(ValidationError):
        parse_agent_output(_valid_payload(suggested_action="teleport"))


def test_parse_agent_output_rejects_confidence_out_of_range() -> None:
    with pytest.raises(ValidationError):
        parse_agent_output(_valid_payload(confidence=1.5))


def test_parse_json_from_fenced_prose() -> None:
    raw = (
        "Here you go:\n```json\n"
        + json.dumps(_valid_payload())
        + "\n```\n"
    )
    parsed = parse_agent_output(raw)
    assert parsed.root_cause == RootCause.QUANTITY_MISMATCH


def test_output_schema_lists_pinned_enums() -> None:
    assert OUTPUT_JSON_SCHEMA["properties"]["root_cause"]["enum"] == list(
        ROOT_CAUSE_VALUES
    )
    assert OUTPUT_JSON_SCHEMA["properties"]["suggested_action"]["enum"] == list(
        SUGGESTED_ACTION_VALUES
    )


def test_all_skill_files_exist_and_are_separate() -> None:
    inventory = skill_inventory()
    names = {row["name"] for row in inventory}
    expected = set(INVESTIGATION_SKILLS) | {MEMORY_WRITER_SKILL}
    assert names == expected
    assert all(row["exists"] for row in inventory)
    skills = load_skills()
    assert [s.name for s in skills] == list(INVESTIGATION_SKILLS)
    prompt = concatenate_skills(skills)
    for name in INVESTIGATION_SKILLS:
        assert name in prompt
    assert MEMORY_WRITER_SKILL not in investigation_skills_prompt()


def test_skill_frontmatter_roundtrip() -> None:
    meta, body = parse_frontmatter(
        "---\nname: demo\ndescription: hello\n---\n\n# Title\n\nBody.\n"
    )
    assert meta["name"] == "demo"
    assert "Body" in body
