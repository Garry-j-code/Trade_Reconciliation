"""Read-only parameterized tools — no free-form SQL, no live market APIs."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

from backend.agent.providers import stub_embedding
from backend.agent.tools import (
    InMemoryStore,
    ToolContext,
    compile_bound_sql,
    dispatch_tool,
    get_corporate_actions,
    get_desk_metadata,
    get_market_session_info,
    get_raw_records,
    get_relevant_memory,
    get_similar_resolved_breaks,
    get_trade_history,
    trade_history_stmt,
)
from backend.data.fetch_market_data import (
    CALENDAR_COLUMNS,
    DIVIDENDS_COLUMNS,
    SPLITS_COLUMNS,
    write_parquet,
)


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    splits = pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "execution_date": "2024-06-10",
                "split_from": 1,
                "split_to": 4,
                "adjustment_type": "split",
                "historical_adjustment_factor": 4.0,
                "id": "s1",
            }
        ],
        columns=list(SPLITS_COLUMNS),
    )
    dividends = pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "ex_dividend_date": "2024-06-11",
                "pay_date": "2024-06-20",
                "record_date": "2024-06-12",
                "declaration_date": "2024-05-01",
                "cash_amount": 0.25,
                "split_adjusted_cash_amount": 0.25,
                "frequency": 4,
                "distribution_type": "cash",
                "historical_adjustment_factor": 1.0,
                "id": "d1",
            }
        ],
        columns=list(DIVIDENDS_COLUMNS),
    )
    calendar = pd.DataFrame(
        [
            {
                "date": "2024-07-04",
                "exchange": "XNYS",
                "name": "Independence Day",
                "status": "closed",
                "open": None,
                "close": None,
            }
        ],
        columns=list(CALENDAR_COLUMNS),
    )
    write_parquet(splits, tmp_path / "splits.parquet")
    write_parquet(dividends, tmp_path / "dividends.parquet")
    write_parquet(calendar, tmp_path / "calendar.parquet")
    return tmp_path


@pytest.fixture
def ctx(cache_dir: Path) -> ToolContext:
    return ToolContext(cache_dir=cache_dir, store=InMemoryStore())


def test_corporate_actions_filters_by_symbol_and_window(ctx: ToolContext) -> None:
    hit = get_corporate_actions(ctx, symbol="AAPL", as_of_date="2024-06-10", window_days=7)
    assert len(hit["splits"]) == 1
    assert hit["splits"][0]["split_to"] == 4
    miss = get_corporate_actions(ctx, symbol="MSFT", as_of_date="2024-06-10")
    assert miss["splits"] == []
    assert miss["dividends"] == []


def test_market_session_info_holiday_and_weekend(ctx: ToolContext) -> None:
    closed = get_market_session_info(ctx, trade_date="2024-07-04")
    assert closed["status"] == "closed"
    weekend = get_market_session_info(ctx, trade_date="2024-06-08")
    assert weekend["is_weekend"] is True
    assert weekend["status"] == "closed"


def test_desk_metadata_static_catalog(ctx: ToolContext) -> None:
    row = get_desk_metadata(ctx, desk_code="EQ-US")
    assert row["found"] is True
    assert row["desk"]["typical_break_rate"] == "low"
    missing = get_desk_metadata(ctx, desk_code="FX-G10")
    assert missing["found"] is False


def test_dispatch_rejects_free_form_sql(ctx: ToolContext) -> None:
    result = dispatch_tool(
        "get_trade_history",
        {"symbol": "AAPL", "sql": "DROP TABLE normalized_trades"},
        ctx,
    )
    assert result["error"] == "Free-form SQL is not allowed"


def test_malicious_symbol_is_rejected_not_interpolated(ctx: ToolContext) -> None:
    result = dispatch_tool(
        "get_trade_history",
        {"symbol": "AAPL'; DROP TABLE normalized_trades;--"},
        ctx,
    )
    assert "error" in result
    assert "SQL" in result["error"] or "identifier" in result["error"]


def test_trade_history_sql_uses_bound_parameters() -> None:
    stmt = trade_history_stmt(
        ticker="AAPL", desk_code="EQ-US", start=date(2024, 1, 1), end=None, cap=10
    )
    sql, params = compile_bound_sql(stmt)
    assert "DROP" not in sql.upper()
    joined = " ".join(str(v) for v in params.values())
    assert "AAPL" in joined
    assert "EQ-US" in joined


def test_trade_history_in_memory_filters(ctx: ToolContext) -> None:
    assert ctx.store is not None
    ctx.store.normalized_trades = [
        {
            "trade_id": "T1",
            "symbol": "AAPL",
            "account": "EQ-US",
            "trade_date": date(2024, 6, 3),
            "quantity": 10,
        },
        {
            "trade_id": "T2",
            "symbol": "MSFT",
            "account": "EQ-US",
            "trade_date": date(2024, 6, 3),
            "quantity": 5,
        },
    ]
    out = get_trade_history(ctx, symbol="AAPL", desk="EQ-US")
    assert out["count"] == 1
    assert out["trades"][0]["trade_id"] == "T1"


def test_raw_records_parameterized_ids(ctx: ToolContext) -> None:
    assert ctx.store is not None
    ctx.store.raw_broker = [
        {"broker_trade_id": "BRK-1", "symbol": "AAPL", "quantity": 10}
    ]
    ctx.store.raw_desk = [{"blotter_id": "DSK-1", "ticker": "AAPL", "qty": 10}]
    out = get_raw_records(
        ctx, broker_trade_ids=["BRK-1"], desk_trade_ids=["DSK-1"]
    )
    assert len(out["broker"]) == 1
    poisoned = dispatch_tool(
        "get_raw_records",
        {"broker_trade_ids": ["BRK-1; DROP TABLE raw_broker_trades"]},
        ctx,
    )
    assert "error" in poisoned


def test_similar_resolved_breaks_only_resolved(ctx: ToolContext) -> None:
    bid = str(uuid4())
    assert ctx.store is not None
    ctx.store.breaks = [
        {
            "break_id": bid,
            "break_type": "price_break",
            "status": "resolved",
            "symbol": "AAPL",
        },
        {
            "break_id": str(uuid4()),
            "break_type": "price_break",
            "status": "open",
            "symbol": "AAPL",
        },
    ]
    ctx.store.suggestions = [{"break_id": bid, "root_cause": "price_mismatch"}]
    out = get_similar_resolved_breaks(ctx, break_type="price_break", symbol="AAPL")
    assert out["count"] == 1


def test_relevant_memory_is_similarity_not_sql(ctx: ToolContext) -> None:
    assert ctx.store is not None
    ctx.embed_fn = stub_embedding
    ctx.store.memory = [
        {
            "memory_id": str(uuid4()),
            "scope": "symbol:AAPL",
            "memory_type": "pattern",
            "content": "AAPL quantity breaks often follow splits.",
            "embedding": stub_embedding("AAPL quantity breaks often follow splits."),
        },
        {
            "memory_id": str(uuid4()),
            "scope": "global",
            "memory_type": "pattern",
            "content": "Unrelated FX noise.",
            "embedding": stub_embedding("Unrelated FX noise."),
        },
    ]
    out = get_relevant_memory(ctx, query_text="AAPL split quantity break")
    assert out["count"] >= 1
    assert "prior" in out["guardrail"].lower()
    assert out["notes"][0]["content"].startswith("AAPL")
