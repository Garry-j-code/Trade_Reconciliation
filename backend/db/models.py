"""SQLAlchemy ORM models for project_plan.md §4 data model.

Raw broker/desk tables keep generator column names untouched.
``normalized_trades`` uses the canonical schema from ``backend.pipeline.normalize``.

``matches`` / ``breaks`` are populated by the deterministic matcher
(``backend.pipeline.matcher``). Agent tables (resolution_suggestions,
audit_log, agent_memory) are written by the agent / HITL flow. ``root_cause``
and ``suggested_action`` are TEXT holding pinned enums from
``backend.agent.enums``. Cluster copies set ``inferred`` on suggestions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all recon tables."""


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Raw legs (generator schemas, untouched)
# ---------------------------------------------------------------------------


class RawBrokerTrade(Base):
    """Ingested broker file row — columns match ``BROKER_COLUMNS``."""

    __tablename__ = "raw_broker_trades"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    broker_trade_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    settlement_date: Mapped[date] = mapped_column(Date, nullable=False)
    settlement_datetime: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_venue: Mapped[str] = mapped_column(String(32), nullable=False)
    pair_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RawDeskTrade(Base):
    """Ingested desk blotter row — columns match ``DESK_COLUMNS``."""

    __tablename__ = "raw_desk_trades"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    blotter_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    settle_date: Mapped[date] = mapped_column(Date, nullable=False)
    settlement_datetime: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    px: Mapped[float] = mapped_column(Float, nullable=False)
    ccy: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    desk_code: Mapped[str] = mapped_column(String(64), nullable=False)
    trader: Mapped[str] = mapped_column(String(64), nullable=False)
    pair_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Canonical trades
# ---------------------------------------------------------------------------


class NormalizedTrade(Base):
    """Both sides in canonical schema; ``source`` is ``broker`` or ``desk``.

    Column mapping (see ``backend.pipeline.normalize``):
      trade_id        ← broker_trade_id | blotter_id
      symbol          ← symbol | ticker
      settlement_date ← settlement_date | settle_date
      quantity        ← quantity | qty
      price           ← price | px
      currency        ← currency | ccy
      account         ← account_id | desk_code
      executing_party ← execution_venue | trader
      pair_id         ← pair_id (preserved for round-trip identity)
    """

    __tablename__ = "normalized_trades"
    __table_args__ = (
        Index("ix_normalized_trades_source_trade_id", "source", "trade_id", unique=True),
        Index("ix_normalized_trades_symbol_date", "symbol", "trade_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    trade_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # broker | desk
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    settlement_date: Mapped[date] = mapped_column(Date, nullable=False)
    settlement_datetime: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    executing_party: Mapped[str] = mapped_column(String(64), nullable=False)
    pair_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Matcher / breaks / agent (empty-ready for later steps)
# ---------------------------------------------------------------------------


class Match(Base):
    """Successful broker↔desk pairing from the deterministic matcher."""

    __tablename__ = "matches"

    match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    broker_trade_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    desk_trade_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pair_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    match_pass: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # exact | tolerance | …
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Break(Base):
    """Unmatched / mismatched trade pair flagged by the pipeline."""

    __tablename__ = "breaks"

    break_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    break_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", index=True
    )
    pair_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    broker_trade_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    desk_trade_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    trade_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    detail: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    cluster_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    suggestions: Mapped[list["ResolutionSuggestion"]] = relationship(
        back_populates="break_", cascade="all, delete-orphan"
    )


class ResolutionSuggestion(Base):
    """Agent output — one row per break. Enums in ``backend.agent.enums``."""

    __tablename__ = "resolution_suggestions"

    suggestion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    break_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("breaks.break_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    root_cause: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_action: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    inferred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    break_: Mapped["Break"] = relationship(back_populates="suggestions")


class AuditLog(Base):
    """Who approved / overrode what — required for every human decision."""

    __tablename__ = "audit_log"

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    break_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("breaks.break_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    suggestion_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resolution_suggestions.suggestion_id", ondelete="SET NULL"),
        nullable=True,
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # approved | overridden | rejected
    override_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    agent_suggestion_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentMemory(Base):
    """Semantic memory notes + embeddings (pgvector). See project_plan.md §6.4."""

    __tablename__ = "agent_memory"

    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid
    )
    scope: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Optional[list[float]]] = mapped_column(Vector(1536), nullable=True)
    source_break_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    audit_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("audit_log.audit_id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    facts: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
