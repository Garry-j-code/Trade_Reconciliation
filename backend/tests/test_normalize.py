"""Unit tests for broker/desk → canonical normalization (no DB)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest

from backend.data.generator import BROKER_COLUMNS, DESK_COLUMNS
from backend.pipeline.normalize import (
    BROKER_REQUIRED,
    CANONICAL_COLUMNS,
    DESK_REQUIRED,
    SOURCE_BROKER,
    SOURCE_DESK,
    NormalizationError,
    combine_normalized,
    empty_normalized,
    normalize_both,
    normalize_broker_trades,
    normalize_desk_trades,
    prepare_normalized_for_parquet,
    require_columns,
)


def _broker_row(**overrides: Any) -> dict[str, Any]:
    base = {
        "broker_trade_id": "BRK-001",
        "symbol": "aapl",
        "trade_date": "2024-06-03",
        "settlement_date": "2024-06-04",
        "side": "buy",
        "quantity": 100.0,
        "price": 190.5,
        "currency": "usd",
        "account_id": "CLR-001",
        "execution_venue": "XNYS",
        "pair_id": "PAIR-001",
    }
    base.update(overrides)
    return base


def _desk_row(**overrides: Any) -> dict[str, Any]:
    base = {
        "blotter_id": "DSK-001",
        "ticker": "aapl",
        "trade_date": "2024-06-03",
        "settle_date": "2024-06-04",
        "side": "buy",
        "qty": 100.0,
        "px": 190.5,
        "ccy": "usd",
        "desk_code": "EQ-ARB",
        "trader": "J.KIM",
        "pair_id": "PAIR-001",
    }
    base.update(overrides)
    return base


@pytest.fixture
def broker_df() -> pd.DataFrame:
    return pd.DataFrame([_broker_row()], columns=list(BROKER_COLUMNS))


@pytest.fixture
def desk_df() -> pd.DataFrame:
    return pd.DataFrame([_desk_row()], columns=list(DESK_COLUMNS))


def test_empty_normalized_has_canonical_columns() -> None:
    df = empty_normalized()
    assert list(df.columns) == list(CANONICAL_COLUMNS)
    assert len(df) == 0


def test_normalize_broker_maps_columns_and_source(broker_df: pd.DataFrame) -> None:
    out = normalize_broker_trades(broker_df)
    assert list(out.columns) == list(CANONICAL_COLUMNS)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["trade_id"] == "BRK-001"
    assert row["source"] == SOURCE_BROKER
    assert row["symbol"] == "AAPL"
    assert row["trade_date"] == date(2024, 6, 3)
    assert row["settlement_date"] == date(2024, 6, 4)
    assert row["side"] == "BUY"
    assert row["quantity"] == 100.0
    assert row["price"] == 190.5
    assert row["currency"] == "USD"
    assert row["account"] == "CLR-001"
    assert row["executing_party"] == "XNYS"
    assert row["pair_id"] == "PAIR-001"
    assert isinstance(row["raw_payload"], dict)
    assert row["raw_payload"]["broker_trade_id"] == "BRK-001"


def test_normalize_desk_maps_columns_and_source(desk_df: pd.DataFrame) -> None:
    out = normalize_desk_trades(desk_df)
    assert list(out.columns) == list(CANONICAL_COLUMNS)
    row = out.iloc[0]
    assert row["trade_id"] == "DSK-001"
    assert row["source"] == SOURCE_DESK
    assert row["symbol"] == "AAPL"
    assert row["settlement_date"] == date(2024, 6, 4)
    assert row["quantity"] == 100.0
    assert row["price"] == 190.5
    assert row["currency"] == "USD"
    assert row["account"] == "EQ-ARB"
    assert row["executing_party"] == "J.KIM"
    assert row["pair_id"] == "PAIR-001"
    assert row["raw_payload"]["blotter_id"] == "DSK-001"


def test_normalize_preserves_pair_id_round_trip(
    broker_df: pd.DataFrame, desk_df: pd.DataFrame
) -> None:
    combined = normalize_both(broker_df, desk_df)
    assert set(combined["pair_id"]) == {"PAIR-001"}
    assert set(combined["source"]) == {SOURCE_BROKER, SOURCE_DESK}
    broker_ids = set(combined.loc[combined["source"] == SOURCE_BROKER, "trade_id"])
    desk_ids = set(combined.loc[combined["source"] == SOURCE_DESK, "trade_id"])
    assert broker_ids == {"BRK-001"}
    assert desk_ids == {"DSK-001"}


def test_normalize_broker_missing_columns_raises(broker_df: pd.DataFrame) -> None:
    bad = broker_df.drop(columns=["quantity", "price"])
    with pytest.raises(NormalizationError, match="broker trades missing"):
        normalize_broker_trades(bad)


def test_normalize_desk_missing_columns_raises(desk_df: pd.DataFrame) -> None:
    bad = desk_df.drop(columns=["qty", "ticker"])
    with pytest.raises(NormalizationError, match="desk trades missing"):
        normalize_desk_trades(bad)


def test_require_columns_reports_all_missing() -> None:
    df = pd.DataFrame({"x": [1]})
    with pytest.raises(NormalizationError) as exc:
        require_columns(df, BROKER_REQUIRED, leg="broker")
    msg = str(exc.value)
    for col in BROKER_REQUIRED:
        assert col in msg


def test_normalize_empty_frames() -> None:
    broker = pd.DataFrame(columns=list(BROKER_COLUMNS))
    desk = pd.DataFrame(columns=list(DESK_COLUMNS))
    assert normalize_broker_trades(broker).empty
    assert normalize_desk_trades(desk).empty
    assert normalize_both(broker, desk).empty


def test_normalize_without_pair_id() -> None:
    broker = pd.DataFrame([_broker_row()]).drop(columns=["pair_id"])
    out = normalize_broker_trades(broker)
    assert out.iloc[0]["pair_id"] is None or pd.isna(out.iloc[0]["pair_id"])


def test_normalize_without_raw_payload(broker_df: pd.DataFrame) -> None:
    out = normalize_broker_trades(broker_df, include_raw_payload=False)
    assert out.iloc[0]["raw_payload"] is None


def test_combine_normalized_stacks_legs(
    broker_df: pd.DataFrame, desk_df: pd.DataFrame
) -> None:
    b = normalize_broker_trades(broker_df)
    d = normalize_desk_trades(desk_df)
    combined = combine_normalized(b, d)
    assert len(combined) == 2
    assert list(combined.columns) == list(CANONICAL_COLUMNS)


def test_prepare_normalized_for_parquet_serializes_payload(
    broker_df: pd.DataFrame,
) -> None:
    out = normalize_broker_trades(broker_df)
    prepared = prepare_normalized_for_parquet(out)
    assert isinstance(prepared.iloc[0]["raw_payload"], str)
    assert prepared.iloc[0]["trade_date"] == "2024-06-03"


def test_desk_required_covers_generator_schema() -> None:
    for col in DESK_REQUIRED:
        assert col in DESK_COLUMNS or col == "settle_date"
    for col in BROKER_REQUIRED:
        assert col in BROKER_COLUMNS
