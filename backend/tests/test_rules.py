"""Unit tests for deterministic matching predicates."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backend.pipeline.rules import (
    as_date,
    empty_splits_frame,
    exact_match_key,
    find_split_hit,
    group_key,
    is_corporate_action_adjusted,
    join_trade_ids,
    parse_trade_ids,
    price_diff_bps,
    price_within_tolerance,
    quantities_equal,
    select_fill_indices,
    settlements_equal,
    split_ratio,
)


def test_as_date_parses_iso_and_rejects_nat() -> None:
    assert as_date("2024-06-03") == date(2024, 6, 3)
    assert as_date(date(2024, 6, 3)) == date(2024, 6, 3)
    assert as_date(None) is None
    assert as_date(float("nan")) is None


def test_price_tolerance_band() -> None:
    assert price_within_tolerance(100.0, 100.04, bps=5.0)  # 4 bps
    assert not price_within_tolerance(100.0, 100.75, bps=5.0)  # 75 bps
    assert price_diff_bps(100.0, 100.0) == 0.0
    with pytest.raises(ValueError):
        price_within_tolerance(1.0, 1.0, bps=-1)


def test_quantities_and_settlements() -> None:
    assert quantities_equal(100.0, 100.0)
    assert not quantities_equal(100.0, 110.0)
    assert settlements_equal("2024-06-05", date(2024, 6, 5))
    assert not settlements_equal("2024-06-05", "2024-06-06")
    assert not settlements_equal(None, "2024-06-05")


def test_keys() -> None:
    row = {
        "symbol": "aapl",
        "side": "buy",
        "trade_date": "2024-06-03",
        "quantity": 100.0,
        "price": 190.5,
        "settlement_date": "2024-06-05",
    }
    assert exact_match_key(row)[0] == "AAPL"
    assert group_key(row) == ("AAPL", "BUY", date(2024, 6, 3))


def test_split_ratio_and_ca_adjusted() -> None:
    assert split_ratio(1, 4) == 4.0
    assert split_ratio(0, 2) is None
    # 4-for-1: broker 400 @ 25 vs desk 100 @ 100
    assert is_corporate_action_adjusted(400.0, 25.0, 100.0, 100.0, 4.0)
    assert not is_corporate_action_adjusted(110.0, 100.0, 100.0, 100.0, 4.0)


def test_find_split_hit_window() -> None:
    splits = pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "execution_date": date(2024, 6, 10),
                "split_from": 1,
                "split_to": 4,
            }
        ]
    )
    hit = find_split_hit(splits, "AAPL", date(2024, 6, 8))
    assert hit is not None
    assert hit.ratio == 4.0
    assert find_split_hit(splits, "AAPL", date(2023, 1, 1)) is None
    assert find_split_hit(empty_splits_frame(), "AAPL", date(2024, 6, 8)) is None


def test_select_fill_indices() -> None:
    assert select_fill_indices([30.0, 70.0], 100.0) == (0, 1)
    assert select_fill_indices([10.0, 20.0, 30.0], 30.0) == (0, 1)
    assert select_fill_indices([50.0], 50.0) is None


def test_join_and_parse_trade_ids() -> None:
    assert join_trade_ids(["BRK-1", "BRK-2"]) == "BRK-1,BRK-2"
    assert parse_trade_ids("BRK-1, BRK-2") == ["BRK-1", "BRK-2"]
    assert parse_trade_ids(None) == []
    assert parse_trade_ids(float("nan")) == []
