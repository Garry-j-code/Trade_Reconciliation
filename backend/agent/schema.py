"""Agent output contract — project_plan.md §6.3. Do not change the JSON shape."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.agent.enums import ROOT_CAUSE_VALUES, RootCause, SuggestedAction

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class EvidenceItem(BaseModel):
    """One tool result the analyst can see on the dashboard."""

    model_config = ConfigDict(extra="ignore")

    tool: str
    result_summary: str


class AgentOutput(BaseModel):
    """Exact §6.3 JSON contract."""

    model_config = ConfigDict(extra="ignore")

    break_id: UUID
    root_cause: RootCause
    confidence: float = Field(ge=0.0, le=1.0)
    explanation: str
    suggested_action: SuggestedAction
    evidence: list[EvidenceItem] = Field(default_factory=list)

    @field_validator("explanation")
    @classmethod
    def explanation_not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("explanation must be non-empty")
        return text

    def to_contract_dict(self) -> dict[str, Any]:
        """Serialize to the dashboard JSON shape (enums as strings)."""
        return {
            "break_id": str(self.break_id),
            "root_cause": self.root_cause.value,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "suggested_action": self.suggested_action.value,
            "evidence": [
                {"tool": item.tool, "result_summary": item.result_summary}
                for item in self.evidence
            ],
        }


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of model text (fences / prose allowed)."""
    stripped = _FENCE_RE.sub("", text.strip()).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("No JSON object found in model output")
    payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Model output JSON must be an object")
    return payload


def parse_agent_output(raw: str | dict[str, Any]) -> AgentOutput:
    """Validate model output against the §6.3 contract and pinned enums.

    Unknown ``root_cause`` strings map to ``other`` (still an enum). Other
    contract failures (bad action, missing fields) still raise.
    """
    if isinstance(raw, str):
        payload = extract_json_object(raw)
    else:
        payload = dict(raw)
    root = payload.get("root_cause")
    if isinstance(root, str) and root not in ROOT_CAUSE_VALUES:
        payload["root_cause"] = RootCause.OTHER.value
    return AgentOutput.model_validate(payload)


OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "break_id",
        "root_cause",
        "confidence",
        "explanation",
        "suggested_action",
        "evidence",
    ],
    "properties": {
        "break_id": {"type": "string", "format": "uuid"},
        "root_cause": {
            "type": "string",
            "enum": [e.value for e in RootCause],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "explanation": {"type": "string"},
        "suggested_action": {
            "type": "string",
            "enum": [e.value for e in SuggestedAction],
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["tool", "result_summary"],
                "properties": {
                    "tool": {"type": "string"},
                    "result_summary": {"type": "string"},
                },
            },
        },
    },
}
