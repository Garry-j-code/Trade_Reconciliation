"""Pydantic request/response models for the recon API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.agent.enums import RootCause, SuggestedAction


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    db: Literal["connected", "unavailable"]


class BreaksByType(BaseModel):
    break_type: str
    count: int


class SummaryResponse(BaseModel):
    """Pair-based dashboard metrics (not normalized-leg or match-row counts)."""

    total_trades: int
    pair_count: int = 0
    broker_leg_count: int = 0
    desk_leg_count: int = 0
    match_count: int
    matched_pair_count: int = 0
    match_row_count: int = 0
    break_count: int
    open_break_count: int
    pct_clean_matched: float
    breaks_by_type: list[BreaksByType]
    notional_at_risk: float


class BreakListItem(BaseModel):
    break_id: UUID
    break_type: str
    status: str
    symbol: str | None = None
    trade_date: date | None = None
    executed_at: datetime | None = None
    pair_id: str | None = None
    desk: str | None = None
    notional_at_risk: float = 0.0
    created_at: datetime | None = None
    last_action: str | None = None
    last_actor: str | None = None
    last_decided_at: datetime | None = None
    last_note: str | None = None


class PaginatedBreaks(BaseModel):
    items: list[BreakListItem]
    total: int
    page: int
    page_size: int


class NormalizedTradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trade_id: str
    source: str
    symbol: str
    trade_date: date
    executed_at: datetime | None = None
    settlement_date: date
    settlement_datetime: datetime | None = None
    side: str
    quantity: float
    price: float
    currency: str
    account: str
    executing_party: str
    pair_id: str | None = None
    raw_payload: dict[str, Any] | None = None


class SideBySide(BaseModel):
    trade_ids: list[str]
    normalized: list[NormalizedTradeOut]
    raw: list[dict[str, Any]]


class EvidenceOut(BaseModel):
    tool: str
    result_summary: str


class SuggestionOut(BaseModel):
    """§6.3 agent contract. Null fields when no suggestion row exists yet."""

    break_id: UUID
    root_cause: str | None = None
    confidence: float | None = None
    explanation: str | None = None
    suggested_action: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    suggestion_id: UUID | None = None
    inferred: bool = False
    tool_calls: int = 0
    review_route: str = "manual_review"


class AuditDecisionOut(BaseModel):
    audit_id: UUID
    actor: str
    action: str
    override_note: str | None = None
    created_at: datetime | None = None
    suggestion_id: UUID | None = None
    root_cause: str | None = None
    suggested_action: str | None = None
    explanation: str | None = None


class BreakDetailResponse(BaseModel):
    break_id: UUID
    break_type: str
    status: str
    symbol: str | None = None
    trade_date: date | None = None
    executed_at: datetime | None = None
    pair_id: str | None = None
    desk: str | None = None
    notional_at_risk: float = 0.0
    detail: dict[str, Any] | None = None
    cluster_id: UUID | None = None
    created_at: datetime | None = None
    broker_side: SideBySide
    desk_side: SideBySide
    suggestion: SuggestionOut
    review_routing: Literal["one_click", "manual_review"]
    decisions: list[AuditDecisionOut] = Field(default_factory=list)


class MatchListItem(BaseModel):
    match_id: UUID
    broker_trade_id: str
    desk_trade_id: str
    pair_id: str | None = None
    match_pass: str
    created_at: datetime | None = None


class PaginatedMatches(BaseModel):
    items: list[MatchListItem]
    total: int
    page: int
    page_size: int


class ReconRunRequest(BaseModel):
    input_dir: str | None = None
    replace: bool = False
    mode: str = "rematch"
    trade_date: date | None = None


class ReconRunResponse(BaseModel):
    broker_rows: int
    desk_rows: int
    normalized_rows: int
    match_count: int
    break_count: int
    breaks_by_type: dict[str, int]
    elapsed_seconds: float
    db_loaded: bool
    investigate_status: str | None = None
    investigate_attempted: int | None = None
    investigate_written: int | None = None
    investigate_failed: int | None = None


class ApprovalRequest(BaseModel):
    actor: str | None = None
    note: str | None = None


class OverrideRequest(BaseModel):
    actor: str | None = None
    note: str = Field(..., min_length=1)


class ApprovalResponse(BaseModel):
    break_id: UUID
    status: str
    action: str
    audit_id: UUID
    suggestion_id: UUID | None = None


# --- step 6 agent endpoints ---


class InvestigateRequest(BaseModel):
    provider: str | None = Field(
        default=None, description="stub | bedrock (default: AGENT_LLM_PROVIDER)"
    )
    tools_enabled: bool = True


class DecisionRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=128)
    note: str | None = None


class DecisionResponse(BaseModel):
    break_id: UUID
    action: str
    status: str
    audit_id: UUID


class BreakOut(BaseModel):
    break_id: UUID
    break_type: str
    status: str
    symbol: str | None = None
    trade_date: str | None = None
    pair_id: str | None = None
    detail: dict[str, Any] | None = None
    cluster_id: UUID | None = None


class AgentSuggestionOut(BaseModel):
    """Filled agent suggestion (enums required)."""

    break_id: UUID
    root_cause: RootCause
    confidence: float
    explanation: str
    suggested_action: SuggestedAction
    evidence: list[EvidenceOut]
    inferred: bool = False
    tool_calls: int = 0
    review_route: str = "manual_review"
    suggestion_id: UUID | None = None
