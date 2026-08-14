"""Unit tests for pair-based dashboard metrics and break sort mapping."""

from __future__ import annotations

from backend.api.crud import (
    BREAK_SORT_FIELDS,
    _break_sort_expression,
    pair_based_summary,
)


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
