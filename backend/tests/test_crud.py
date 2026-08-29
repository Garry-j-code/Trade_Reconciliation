"""Unit tests for pair-based dashboard metrics and break sort mapping."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from backend.api.crud import (
    BREAK_SORT_FIELDS,
    _apply_break_filters,
    _break_sort_expression,
    pair_based_summary,
    parse_break_status_filter,
    resolve_date_range,
    trade_date_clauses,
)
from backend.db.models import Break, NormalizedTrade


def test_pair_based_summary_uses_unique_pairs_not_legs_or_match_rows() -> None:
    stats = pair_based_summary(
        pair_count=360,
        broker_leg_count=393,
        desk_leg_count=342,
        matched_pair_count=280,
        match_row_count=321,
        break_count=80,
        open_break_count=80,
        breaks_by_type=[{"break_type": "price_break", "count": 12}],
        notional_at_risk=1_250_000.0,
    )
    assert stats["total_trades"] == 360
    assert stats["pair_count"] == 360
    assert stats["match_count"] == 280
    assert stats["matched_pair_count"] == 280
    assert stats["match_row_count"] == 321
    assert stats["broker_leg_count"] == 393
    assert stats["desk_leg_count"] == 342
    assert stats["break_count"] == 80
    assert stats["pct_clean_matched"] == round(100.0 * 280 / 360, 4)
    assert stats["pct_clean_matched"] != round(100.0 * 321 / 735, 4)
    assert stats["pct_clean_matched"] != round(100.0 * 321 / 360, 4)


def test_pair_based_summary_zero_pairs() -> None:
    stats = pair_based_summary(
        pair_count=0,
        broker_leg_count=0,
        desk_leg_count=0,
        matched_pair_count=0,
        match_row_count=0,
        break_count=0,
        open_break_count=0,
        breaks_by_type=[],
        notional_at_risk=0.0,
    )
    assert stats["pct_clean_matched"] == 0.0
    assert stats["total_trades"] == 0


def test_resolve_date_range_empty_is_all_dates() -> None:
    assert resolve_date_range() == (None, None)


def test_resolve_date_range_single_trade_date_maps_to_one_day() -> None:
    day = date(2024, 6, 3)
    assert resolve_date_range(trade_date=day) == (day, day)


def test_resolve_date_range_inclusive_window() -> None:
    start, end = resolve_date_range(from_date=date(2024, 6, 3), to_date=date(2024, 6, 10))
    assert start == date(2024, 6, 3)
    assert end == date(2024, 6, 10)


def test_resolve_date_range_rejects_inverted() -> None:
    with pytest.raises(ValueError, match="from_date must be on or before to_date"):
        resolve_date_range(from_date=date(2024, 6, 10), to_date=date(2024, 6, 3))


def test_trade_date_clauses_inclusive_and_empty() -> None:
    assert trade_date_clauses(Break.trade_date, None, None) == []
    clauses = trade_date_clauses(Break.trade_date, date(2024, 6, 3), date(2024, 6, 10))
    assert len(clauses) == 2


def _compile(stmt: object) -> str:
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def test_break_filters_inclusive_trade_date_range() -> None:
    stmt = _apply_break_filters(
        select(Break),
        desk=None,
        symbol=None,
        break_type=None,
        trade_date=None,
        date_from=date(2024, 6, 3),
        date_to=date(2024, 6, 10),
        status=None,
    )
    sql = _compile(stmt)
    assert "trade_date" in sql
    assert ">= '2024-06-03'" in sql
    assert "<= '2024-06-10'" in sql


def test_break_filters_empty_range_is_unfiltered() -> None:
    stmt = _apply_break_filters(
        select(Break),
        desk=None,
        symbol=None,
        break_type=None,
        trade_date=None,
        date_from=None,
        date_to=None,
        status=None,
    )
    sql = _compile(stmt)
    assert "WHERE" not in sql
    assert ">=" not in sql
    assert "<=" not in sql


def test_break_filters_legacy_single_date_is_inclusive_one_day() -> None:
    stmt = _apply_break_filters(
        select(Break),
        desk=None,
        symbol=None,
        break_type=None,
        trade_date=date(2024, 6, 3),
        date_from=None,
        date_to=None,
        status="open",
    )
    sql = _compile(stmt)
    assert ">= '2024-06-03'" in sql
    assert "<= '2024-06-03'" in sql
    assert "status" in sql


def test_parse_break_status_filter_all_and_known() -> None:
    assert parse_break_status_filter(None) is None
    assert parse_break_status_filter("all") is None
    assert parse_break_status_filter("resolved") == "resolved"
    assert parse_break_status_filter("rejected") == "rejected"
    assert parse_break_status_filter("overridden") == "overridden"
    assert parse_break_status_filter("open") == "open"
    with pytest.raises(ValueError, match="status"):
        parse_break_status_filter("closed")


def test_break_filters_resolved_and_all_status() -> None:
    resolved = _compile(
        _apply_break_filters(
            select(Break),
            desk=None,
            symbol=None,
            break_type=None,
            trade_date=None,
            date_from=None,
            date_to=None,
            status="resolved",
        )
    )
    assert "resolved" in resolved
    for status in ("rejected", "overridden", "open"):
        sql = _compile(
            _apply_break_filters(
                select(Break),
                desk=None,
                symbol=None,
                break_type="price_break",
                trade_date=None,
                date_from=date(2024, 6, 3),
                date_to=date(2024, 6, 10),
                status=status,
            )
        )
        assert status in sql
        assert "price_break" in sql
        assert ">= '2024-06-03'" in sql
    unfiltered = _compile(
        _apply_break_filters(
            select(Break),
            desk=None,
            symbol=None,
            break_type=None,
            trade_date=None,
            date_from=None,
            date_to=None,
            status="all",
        )
    )
    assert "WHERE" not in unfiltered


def test_trade_date_clauses_apply_to_normalized_trades() -> None:
    clauses = trade_date_clauses(
        NormalizedTrade.trade_date, date(2024, 6, 3), date(2024, 6, 3)
    )
    stmt = select(NormalizedTrade).where(*clauses)
    sql = _compile(stmt)
    assert ">= '2024-06-03'" in sql
    assert "<= '2024-06-03'" in sql


def test_break_sort_fields_cover_table_columns() -> None:
    assert BREAK_SORT_FIELDS == (
        "break_type",
        "status",
        "desk",
        "symbol",
        "trade_date",
        "notional",
    )
    for field in BREAK_SORT_FIELDS:
        expr = _break_sort_expression(field)
        assert expr is not None
