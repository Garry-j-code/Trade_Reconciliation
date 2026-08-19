"""Unit tests for synthetic trade generation (fixtures only — no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from backend.data.fetch_market_data import (
    BARS_COLUMNS,
    CALENDAR_COLUMNS,
    SPLITS_COLUMNS,
    write_parquet,
)
from backend.data.generator import (
    BREAK_CLEAN,
    BREAK_CORPORATE_ACTION,
    BREAK_DUPLICATE,
    BREAK_MISSING_BROKER,
    BREAK_MISSING_DESK,
    BREAK_PRICE,
    BREAK_QUANTITY,
    BREAK_SETTLEMENT,
    BREAK_SPLIT_FILL,
    BROKER_COLUMNS,
    DESK_COLUMNS,
    GROUND_TRUTH_COLUMNS,
    BreakRates,
    GeneratorConfig,
    MarketCache,
    closed_market_dates,
    generate_corporate_action_breaks,
    generate_trades,
    last_cached_us_session,
    last_completed_us_session,
    load_market_cache,
    next_business_day,
    parse_iso_date,
    prior_us_sessions,
    run_generate,
    seed_for_trade_date,
    settlement_date_for,
    split_ratio,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bars_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(BARS_COLUMNS))


def _splits_frame(rows: list[dict[str, Any]] | None = None) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=list(SPLITS_COLUMNS))
    return pd.DataFrame(rows, columns=list(SPLITS_COLUMNS))


def _calendar_frame(rows: list[dict[str, Any]] | None = None) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=list(CALENDAR_COLUMNS))
    return pd.DataFrame(rows, columns=list(CALENDAR_COLUMNS))


def _bar(
    ticker: str,
    day: str,
    *,
    close: float = 100.0,
    volume: float = 1_000_000,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "date": day,
        "open": close - 1,
        "high": close + 2,
        "low": close - 2,
        "close": close,
        "volume": volume,
        "vwap": close,
        "transactions": 5000,
    }


def build_tiny_cache(tmp_path: Path) -> Path:
    """Minimal on-disk cache matching fetch_market_data layout."""
    cache = tmp_path / "cache"
    bars_dir = cache / "bars"
    bars_dir.mkdir(parents=True)

    aapl_days = [
        _bar("AAPL", "2024-05-28", close=190.0),
        _bar("AAPL", "2024-05-29", close=191.0),
        _bar("AAPL", "2024-05-30", close=192.0),
        _bar("AAPL", "2024-05-31", close=193.0),
        _bar("AAPL", "2024-06-03", close=194.0),
    ]
    msft_days = [
        _bar("MSFT", "2024-05-28", close=410.0),
        _bar("MSFT", "2024-05-29", close=411.0),
        _bar("MSFT", "2024-05-30", close=412.0),
        _bar("MSFT", "2024-05-31", close=413.0),
        _bar("MSFT", "2024-06-03", close=414.0),
    ]
    write_parquet(_bars_frame(aapl_days), bars_dir / "AAPL.parquet")
    write_parquet(_bars_frame(msft_days), bars_dir / "MSFT.parquet")

    # 4-for-1 split on AAPL (real factor shape from Massive columns)
    splits = _splits_frame(
        [
            {
                "ticker": "AAPL",
                "execution_date": "2024-06-10",
                "split_from": 1,
                "split_to": 4,
                "adjustment_type": "forward",
                "historical_adjustment_factor": 0.25,
                "id": "split-aapl-test",
            }
        ]
    )
    write_parquet(splits, cache / "splits.parquet")

    # Memorial Day closed — settlement should skip it
    calendar = _calendar_frame(
        [
            {
                "date": "2024-05-27",
                "exchange": "NYSE",
                "name": "Memorial Day",
                "status": "closed",
                "open": None,
                "close": None,
            }
        ]
    )
    write_parquet(calendar, cache / "calendar.parquet")
    return cache


@pytest.fixture
def tiny_cache(tmp_path: Path) -> Path:
    return build_tiny_cache(tmp_path)


@pytest.fixture
def memory_cache(tiny_cache: Path) -> MarketCache:
    return load_market_cache(tiny_cache)


# ---------------------------------------------------------------------------
# Helpers / calendar
# ---------------------------------------------------------------------------


def test_split_ratio() -> None:
    assert split_ratio(1, 4) == 4.0
    assert split_ratio(2, 1) == 0.5
    assert split_ratio(0, 4) is None
    assert split_ratio(None, 4) is None


def test_settlement_skips_weekend_and_holiday() -> None:
    # Friday 2024-05-24 → next business day is Tue 2024-05-28 (Mon holiday)
    closed = {date(2024, 5, 27)}
    assert settlement_date_for(date(2024, 5, 24), closed=closed) == date(2024, 5, 28)
    assert next_business_day(date(2024, 5, 24), closed=closed, offset=1) == date(
        2024, 5, 28
    )


def test_closed_market_dates_ignores_early_close() -> None:
    cal = _calendar_frame(
        [
            {
                "date": "2024-07-03",
                "exchange": "NYSE",
                "name": "Day Before Independence Day",
                "status": "early-close",
                "open": "09:30",
                "close": "13:00",
            },
            {
                "date": "2024-07-04",
                "exchange": "NYSE",
                "name": "Independence Day",
                "status": "closed",
                "open": None,
                "close": None,
            },
        ]
    )
    assert closed_market_dates(cal) == {date(2024, 7, 4)}


def test_load_market_cache(tiny_cache: Path) -> None:
    cache = load_market_cache(tiny_cache)
    assert set(cache.bars["ticker"].unique()) == {"AAPL", "MSFT"}
    assert len(cache.splits) == 1
    assert cache.splits.iloc[0]["ticker"] == "AAPL"
    assert not cache.calendar.empty


def test_load_market_cache_missing_bars(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Bars directory"):
        load_market_cache(tmp_path / "nope")


def test_load_market_cache_symbol_filter(tiny_cache: Path) -> None:
    cache = load_market_cache(tiny_cache, symbols=["AAPL"])
    assert set(cache.bars["ticker"].unique()) == {"AAPL"}


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def test_generate_trades_schemas_and_clean_pairs(memory_cache: MarketCache) -> None:
    config = GeneratorConfig(
        cache_dir=memory_cache.cache_dir,
        n_trades=40,
        seed=7,
        rates=BreakRates(
            missing_broker=0.0,
            missing_desk=0.0,
            price_break=0.0,
            quantity_break=0.0,
            duplicate=0.0,
            settlement_date_mismatch=0.0,
            split_fill=0.0,
        ),
        max_corporate_action_breaks=0,
    )
    result = generate_trades(memory_cache, config)
    assert list(result.broker.columns) == list(BROKER_COLUMNS)
    assert list(result.desk.columns) == list(DESK_COLUMNS)
    assert list(result.ground_truth.columns) == list(GROUND_TRUTH_COLUMNS)
    assert len(result.ground_truth) == 40
    assert set(result.ground_truth["break_type"]) == {BREAK_CLEAN}
    # One broker + one desk per clean pair
    assert len(result.broker) == 40
    assert len(result.desk) == 40
    # Matching economics for a sample pair
    pair_id = result.ground_truth.iloc[0]["pair_id"]
    b = result.broker[result.broker["pair_id"] == pair_id].iloc[0]
    d = result.desk[result.desk["pair_id"] == pair_id].iloc[0]
    assert b["symbol"] == d["ticker"]
    assert b["quantity"] == d["qty"]
    assert b["price"] == d["px"]
    assert b["trade_date"] == d["trade_date"]
    assert b["executed_at"] == d["executed_at"]
    assert pd.notna(b["executed_at"])
    assert b["settlement_date"] == d["settle_date"]
    assert b["side"] == d["side"]
    exec_at = pd.Timestamp(b["executed_at"])
    assert exec_at.tzinfo is not None
    ny = exec_at.tz_convert("America/New_York")
    minutes = ny.hour * 60 + ny.minute
    assert 9 * 60 + 30 <= minutes < 16 * 60
    settle_at = pd.Timestamp(b["settlement_datetime"])
    settle_ny = settle_at.tz_convert("America/New_York")
    assert settle_ny.hour == 16
    assert settle_ny.minute == 0
    assert str(b["settlement_datetime"])[:10] == b["settlement_date"]


def test_injected_break_types_present(memory_cache: MarketCache) -> None:
    config = GeneratorConfig(
        cache_dir=memory_cache.cache_dir,
        n_trades=200,
        seed=99,
        rates=BreakRates(
            missing_broker=0.08,
            missing_desk=0.08,
            price_break=0.08,
            quantity_break=0.08,
            duplicate=0.08,
            settlement_date_mismatch=0.08,
            split_fill=0.08,
        ),
        max_corporate_action_breaks=0,
    )
    result = generate_trades(memory_cache, config)
    types = set(result.ground_truth["break_type"])
    for expected in (
        BREAK_MISSING_BROKER,
        BREAK_MISSING_DESK,
        BREAK_PRICE,
        BREAK_QUANTITY,
        BREAK_DUPLICATE,
        BREAK_SETTLEMENT,
        BREAK_SPLIT_FILL,
        BREAK_CLEAN,
    ):
        assert expected in types, f"missing {expected} in {types}"


def test_missing_broker_has_desk_only(memory_cache: MarketCache) -> None:
    config = GeneratorConfig(
        cache_dir=memory_cache.cache_dir,
        n_trades=50,
        seed=1,
        rates=BreakRates(
            missing_broker=1.0,
            missing_desk=0.0,
            price_break=0.0,
            quantity_break=0.0,
            duplicate=0.0,
            settlement_date_mismatch=0.0,
            split_fill=0.0,
        ),
        max_corporate_action_breaks=0,
    )
    result = generate_trades(memory_cache, config)
    assert set(result.ground_truth["break_type"]) == {BREAK_MISSING_BROKER}
    assert result.broker.empty
    assert len(result.desk) == 50


def test_price_break_diverges(memory_cache: MarketCache) -> None:
    config = GeneratorConfig(
        cache_dir=memory_cache.cache_dir,
        n_trades=20,
        seed=2,
        price_break_bps=100.0,
        rates=BreakRates(
            missing_broker=0.0,
            missing_desk=0.0,
            price_break=1.0,
            quantity_break=0.0,
            duplicate=0.0,
            settlement_date_mismatch=0.0,
            split_fill=0.0,
        ),
        max_corporate_action_breaks=0,
    )
    result = generate_trades(memory_cache, config)
    for _, row in result.ground_truth.iterrows():
        b = result.broker[result.broker["pair_id"] == row["pair_id"]].iloc[0]
        d = result.desk[result.desk["pair_id"] == row["pair_id"]].iloc[0]
        assert d["px"] != b["price"]
        assert abs(d["px"] / b["price"] - 1.01) < 1e-6


def test_quantity_break_diverges(memory_cache: MarketCache) -> None:
    config = GeneratorConfig(
        cache_dir=memory_cache.cache_dir,
        n_trades=15,
        seed=3,
        rates=BreakRates(
            missing_broker=0.0,
            missing_desk=0.0,
            price_break=0.0,
            quantity_break=1.0,
            duplicate=0.0,
            settlement_date_mismatch=0.0,
            split_fill=0.0,
        ),
        max_corporate_action_breaks=0,
    )
    result = generate_trades(memory_cache, config)
    for _, row in result.ground_truth.iterrows():
        b = result.broker[result.broker["pair_id"] == row["pair_id"]].iloc[0]
        d = result.desk[result.desk["pair_id"] == row["pair_id"]].iloc[0]
        assert d["qty"] != b["quantity"]


def test_duplicate_creates_two_broker_rows(memory_cache: MarketCache) -> None:
    config = GeneratorConfig(
        cache_dir=memory_cache.cache_dir,
        n_trades=10,
        seed=4,
        rates=BreakRates(
            missing_broker=0.0,
            missing_desk=0.0,
            price_break=0.0,
            quantity_break=0.0,
            duplicate=1.0,
            settlement_date_mismatch=0.0,
            split_fill=0.0,
        ),
        max_corporate_action_breaks=0,
    )
    result = generate_trades(memory_cache, config)
    assert len(result.broker) == 20
    assert len(result.desk) == 10
    for _, row in result.ground_truth.iterrows():
        bids = row["broker_trade_ids"].split(",")
        assert len(bids) == 2


def test_settlement_mismatch(memory_cache: MarketCache) -> None:
    config = GeneratorConfig(
        cache_dir=memory_cache.cache_dir,
        n_trades=10,
        seed=5,
        rates=BreakRates(
            missing_broker=0.0,
            missing_desk=0.0,
            price_break=0.0,
            quantity_break=0.0,
            duplicate=0.0,
            settlement_date_mismatch=1.0,
            split_fill=0.0,
        ),
        max_corporate_action_breaks=0,
    )
    result = generate_trades(memory_cache, config)
    for _, row in result.ground_truth.iterrows():
        b = result.broker[result.broker["pair_id"] == row["pair_id"]].iloc[0]
        d = result.desk[result.desk["pair_id"] == row["pair_id"]].iloc[0]
        assert b["settlement_date"] != d["settle_date"]


def test_split_fill_sums_to_desk_qty(memory_cache: MarketCache) -> None:
    config = GeneratorConfig(
        cache_dir=memory_cache.cache_dir,
        n_trades=25,
        seed=6,
        rates=BreakRates(
            missing_broker=0.0,
            missing_desk=0.0,
            price_break=0.0,
            quantity_break=0.0,
            duplicate=0.0,
            settlement_date_mismatch=0.0,
            split_fill=1.0,
        ),
        max_corporate_action_breaks=0,
    )
    result = generate_trades(memory_cache, config)
    for _, row in result.ground_truth.iterrows():
        fills = result.broker[result.broker["pair_id"] == row["pair_id"]]
        desk = result.desk[result.desk["pair_id"] == row["pair_id"]].iloc[0]
        assert len(fills) >= 2
        assert abs(fills["quantity"].sum() - desk["qty"]) < 1e-6


def test_corporate_action_uses_real_split_factor(memory_cache: MarketCache) -> None:
    rng = np.random.default_rng(11)
    config = GeneratorConfig(cache_dir=memory_cache.cache_dir, seed=11)
    closed = closed_market_dates(memory_cache.calendar)
    b_rows, d_rows, t_rows = generate_corporate_action_breaks(
        memory_cache, rng=rng, closed=closed, config=config, max_breaks=None
    )
    assert len(t_rows) == 1
    assert t_rows[0]["break_type"] == BREAK_CORPORATE_ACTION
    assert "ratio=4.0" in t_rows[0]["detail"]

    broker = b_rows[0]
    desk = d_rows[0]
    # Broker adjusted: qty * 4, price / 4 (allow rounding to 4 dp)
    assert abs(broker["quantity"] / desk["qty"] - 4.0) < 1e-6
    assert abs(desk["px"] / broker["price"] - 4.0) < 1e-3


def test_corporate_action_included_in_generate_trades(
    memory_cache: MarketCache,
) -> None:
    config = GeneratorConfig(
        cache_dir=memory_cache.cache_dir,
        n_trades=10,
        seed=8,
        rates=BreakRates(
            missing_broker=0.0,
            missing_desk=0.0,
            price_break=0.0,
            quantity_break=0.0,
            duplicate=0.0,
            settlement_date_mismatch=0.0,
            split_fill=0.0,
        ),
        max_corporate_action_breaks=None,
    )
    result = generate_trades(memory_cache, config)
    ca = result.ground_truth[
        result.ground_truth["break_type"] == BREAK_CORPORATE_ACTION
    ]
    assert len(ca) == 1
    assert result.summary["corporate_action_splits_used"] == 1


def test_prices_come_from_bar_range(memory_cache: MarketCache) -> None:
    config = GeneratorConfig(
        cache_dir=memory_cache.cache_dir,
        n_trades=30,
        seed=12,
        rates=BreakRates(
            missing_broker=0.0,
            missing_desk=0.0,
            price_break=0.0,
            quantity_break=0.0,
            duplicate=0.0,
            settlement_date_mismatch=0.0,
            split_fill=0.0,
        ),
        max_corporate_action_breaks=0,
    )
    result = generate_trades(memory_cache, config)
    bars = memory_cache.bars.copy()
    bars["date_str"] = bars["date"].map(
        lambda d: d.isoformat() if hasattr(d, "isoformat") else str(d)
    )
    for _, b in result.broker.iterrows():
        day_bars = bars[
            (bars["ticker"] == b["symbol"]) & (bars["date_str"] == b["trade_date"])
        ]
        assert not day_bars.empty
        low = float(day_bars.iloc[0]["low"])
        high = float(day_bars.iloc[0]["high"])
        assert low - 1e-6 <= float(b["price"]) <= high + 1e-6


def test_deterministic_seed(memory_cache: MarketCache) -> None:
    config = GeneratorConfig(
        cache_dir=memory_cache.cache_dir,
        n_trades=25,
        seed=123,
        max_corporate_action_breaks=0,
    )
    a = generate_trades(memory_cache, config)
    b = generate_trades(memory_cache, config)
    pd.testing.assert_frame_equal(a.broker, b.broker)
    pd.testing.assert_frame_equal(a.desk, b.desk)
    pd.testing.assert_frame_equal(a.ground_truth, b.ground_truth)


def test_write_and_run_generate(tiny_cache: Path, tmp_path: Path) -> None:
    out = tmp_path / "generated"
    config = GeneratorConfig(
        cache_dir=tiny_cache,
        output_dir=out,
        n_trades=20,
        seed=42,
        max_corporate_action_breaks=1,
    )
    summary = run_generate(config)
    assert (out / "broker_trades.parquet").is_file()
    assert (out / "desk_trades.parquet").is_file()
    assert (out / "ground_truth.parquet").is_file()
    assert (out / "generation_summary.json").is_file()
    payload = json.loads((out / "generation_summary.json").read_text())
    assert payload["n_pairs"] == summary["n_pairs"]
    broker = pd.read_parquet(out / "broker_trades.parquet")
    assert list(broker.columns) == list(BROKER_COLUMNS)


def test_generate_trades_uses_only_in_memory_cache(memory_cache: MarketCache) -> None:
    """generate_trades consumes MarketCache only — no live market API."""
    result = generate_trades(
        memory_cache,
        GeneratorConfig(
            cache_dir=memory_cache.cache_dir,
            n_trades=5,
            seed=0,
            max_corporate_action_breaks=0,
        ),
    )
    assert result.summary["n_pairs"] == 5
    assert result.summary["cache_dir"] == str(memory_cache.cache_dir)


def test_seed_for_trade_date_is_stable() -> None:
    a = seed_for_trade_date(42, date(2026, 8, 13))
    b = seed_for_trade_date(42, date(2026, 8, 13))
    c = seed_for_trade_date(42, date(2026, 8, 14))
    assert a == b
    assert a != c
    assert parse_iso_date("2024-06-03") == date(2024, 6, 3)


def test_last_completed_us_session_skips_weekend_and_holiday() -> None:
    closed = {date(2024, 5, 27)}
    assert last_completed_us_session(date(2024, 5, 27), closed) == date(2024, 5, 24)
    assert last_completed_us_session(date(2024, 6, 2), closed) == date(2024, 5, 31)
    days = prior_us_sessions(3, as_of=date(2024, 6, 3), closed=closed)
    assert days[-1] == date(2024, 6, 3)
    assert date(2024, 5, 27) not in days


def test_last_cached_us_session_lags_to_newest_bars() -> None:
    """The provider publishes T-1, so the just-closed session has no bars yet."""
    bars = pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL", "MSFT"],
            "date": ["2024-05-30", "2024-05-31", "2024-05-31"],
        }
    )
    # 2024-06-03 is a Monday; 05-31 is the newest session actually cached.
    assert last_cached_us_session(bars, date(2024, 6, 3), set()) == date(2024, 5, 31)


def test_last_cached_us_session_ignores_future_and_closed_sessions() -> None:
    bars = pd.DataFrame(
        {
            "ticker": ["AAPL", "AAPL", "AAPL"],
            "date": ["2024-05-24", "2024-05-27", "2024-06-10"],
        }
    )
    closed = {date(2024, 5, 27)}
    # 05-27 is a holiday and 06-10 is past as_of, so 05-24 is the anchor.
    assert last_cached_us_session(bars, date(2024, 5, 28), closed) == date(2024, 5, 24)


def test_last_cached_us_session_raises_on_empty_cache() -> None:
    with pytest.raises(ValueError, match="no bars"):
        last_cached_us_session(pd.DataFrame(), date(2024, 6, 3), set())
    with pytest.raises(ValueError, match="no session on or before"):
        last_cached_us_session(
            pd.DataFrame({"ticker": ["AAPL"], "date": ["2024-07-01"]}),
            date(2024, 6, 3),
            set(),
        )


def test_dated_generate_is_idempotent(memory_cache: MarketCache) -> None:
    cfg = GeneratorConfig(
        cache_dir=memory_cache.cache_dir,
        n_trades=12,
        seed=42,
        trade_date=date(2024, 6, 3),
        max_corporate_action_breaks=0,
    )
    a = generate_trades(memory_cache, cfg)
    b = generate_trades(memory_cache, cfg)
    pd.testing.assert_frame_equal(a.broker, b.broker)
    pd.testing.assert_frame_equal(a.desk, b.desk)
    assert set(a.broker["trade_date"].astype(str)) == {"2024-06-03"}
    ids = sorted(a.broker["broker_trade_id"].tolist())
    assert ids == sorted(b.broker["broker_trade_id"].tolist())


def test_dated_generate_empty_on_closed_day(memory_cache: MarketCache) -> None:
    with pytest.raises(ValueError, match="No eligible bars"):
        generate_trades(
            memory_cache,
            GeneratorConfig(
                cache_dir=memory_cache.cache_dir,
                n_trades=5,
                seed=42,
                trade_date=date(2024, 5, 27),
                max_corporate_action_breaks=0,
            ),
        )
