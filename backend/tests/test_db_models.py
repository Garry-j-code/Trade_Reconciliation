"""Smoke tests that SQLAlchemy models import and metadata is complete."""

from __future__ import annotations

from backend.db.models import (
    AgentMemory,
    AuditLog,
    Base,
    Break,
    Match,
    NormalizedTrade,
    RawBrokerTrade,
    RawDeskTrade,
    ResolutionSuggestion,
)


def test_all_section4_tables_registered() -> None:
    names = set(Base.metadata.tables)
    expected = {
        "raw_broker_trades",
        "raw_desk_trades",
        "normalized_trades",
        "matches",
        "breaks",
        "resolution_suggestions",
        "audit_log",
        "agent_memory",
    }
    assert expected <= names


def test_normalized_trade_has_source_and_pair_id() -> None:
    cols = {c.name for c in NormalizedTrade.__table__.columns}
    assert {"trade_id", "source", "pair_id", "account", "executing_party"} <= cols


def test_raw_legs_preserve_generator_column_names() -> None:
    broker_cols = {c.name for c in RawBrokerTrade.__table__.columns}
    desk_cols = {c.name for c in RawDeskTrade.__table__.columns}
    assert "broker_trade_id" in broker_cols
    assert "execution_venue" in broker_cols
    assert "blotter_id" in desk_cols
    assert "settle_date" in desk_cols
    assert "qty" in desk_cols


def test_audit_log_break_id_nullable_on_delete() -> None:
    col = AuditLog.__table__.c.break_id
    assert col.nullable is True


def test_resolution_suggestion_has_inferred_flag() -> None:
    cols = {c.name for c in ResolutionSuggestion.__table__.columns}
    assert "inferred" in cols
    assert "root_cause" in cols
    assert "suggested_action" in cols
