"""Chart Others rollup and display-category helper. No DB, no LLM."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from backend.api.chart_types import (
    OTHERS_CHART_KEY,
    UNCLASSIFIED_DISPLAY_TYPE,
    displayed_break_category,
    parse_break_type_filter,
    rollup_chart_types,
)
from backend.db.models import Break, ResolutionSuggestion


def test_rollup_merges_share_below_five_percent() -> None:
    rolled = rollup_chart_types(
        {
            "price_mismatch": 90,
            "quantity_mismatch": 4,
            "duplicate_booking": 3,
            "unclassified": 3,
        }
    )
    keys = [row["break_type"] for row in rolled.chart]
    assert keys == ["price_mismatch", OTHERS_CHART_KEY]
    others = next(row for row in rolled.chart if row["break_type"] == OTHERS_CHART_KEY)
    assert others["count"] == 10
    assert rolled.others_members == [
        "duplicate_booking",
        "quantity_mismatch",
        "unclassified",
    ]
    assert "quantity_mismatch" in rolled.options
    assert OTHERS_CHART_KEY not in rolled.options


def test_rollup_merges_count_below_three() -> None:
    rolled = rollup_chart_types(
        {"price_mismatch": 80, "quantity_mismatch": 18, "other": 2}
    )
    assert [row["break_type"] for row in rolled.chart] == [
        "price_mismatch",
        "quantity_mismatch",
        OTHERS_CHART_KEY,
    ]
    others = next(row for row in rolled.chart if row["break_type"] == OTHERS_CHART_KEY)
    assert others["count"] == 2
    assert rolled.others_members == ["other"]


def test_rollup_keeps_types_at_or_above_five_percent_and_three_count() -> None:
    rolled = rollup_chart_types(
        {"price_mismatch": 50, "quantity_mismatch": 30, "unclassified": 20}
    )
    assert rolled.others_members == []
    assert [row["break_type"] for row in rolled.chart] == [
        "price_mismatch",
        "quantity_mismatch",
        "unclassified",
    ]


def test_rollup_all_small_types_keeps_top_eight() -> None:
    counts = {f"type_{i:02d}": 1 for i in range(25)}
    rolled = rollup_chart_types(counts)
    named = [row["break_type"] for row in rolled.chart if row["break_type"] != OTHERS_CHART_KEY]
    assert len(named) == 8
    assert len(rolled.others_members) == 17
    assert sum(row["count"] for row in rolled.chart) == 25


def test_display_category_unclassified_until_investigated() -> None:
    brk = Break(break_id=uuid4(), break_type="quantity_break", status="open")
    brk.suggestions = [
        ResolutionSuggestion(
            suggestion_id=uuid4(),
            break_id=brk.break_id,
            root_cause="corporate_action_timing",
            confidence=0.7,
            explanation="split",
            suggested_action="wait_for_corporate_action",
            created_at=datetime.now(timezone.utc),
        )
    ]
    assert displayed_break_category(brk) == "corporate_action_timing"
    assert displayed_break_category(Break(break_type="price_break", suggestions=[])) == (
        UNCLASSIFIED_DISPLAY_TYPE
    )


def test_display_category_maps_unknown_stored_cause_to_other() -> None:
    brk = Break(break_id=uuid4(), break_type="price_break", status="open")
    brk.suggestions = [
        ResolutionSuggestion(
            suggestion_id=uuid4(),
            break_id=brk.break_id,
            root_cause="fat_finger",
            confidence=0.5,
            explanation="n",
            suggested_action="escalate_to_ops",
            created_at=datetime.now(timezone.utc),
        )
    ]
    assert displayed_break_category(brk) == "other"


def test_parse_break_type_filter_splits_and_others() -> None:
    assert parse_break_type_filter(None) == []
    assert parse_break_type_filter("unclassified") == ["unclassified"]
    assert parse_break_type_filter("a,b, c") == ["a", "b", "c"]
    assert parse_break_type_filter(OTHERS_CHART_KEY) == [OTHERS_CHART_KEY]
