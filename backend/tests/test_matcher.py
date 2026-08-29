"""Unit tests for the deterministic matcher (fixtures only — no Massive)."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pandas as pd
import pytest

from backend.db.models import NormalizedTrade
from backend.pipeline import matcher as matcher_mod
from backend.pipeline.matcher import (
    BREAK_COLUMNS,
    MATCH_COLUMNS,
    MatchError,
    break_row_to_orm,
    break_identity,
    build_arg_parser,
    default_matched_output_dir,
    empty_breaks_frame,
    empty_matches_frame,
    load_splits_cache,
    match_normalized_trades,
    match_row_to_orm,
    match_trades,
    normalized_orm_to_record,
    prepare_breaks_for_parquet,
    prepare_matches_for_parquet,
    read_normalized_parquet,
    resolve_match_paths,
    run_match,
    split_legs,
    stable_break_id,
    summarize_match_frames,
    write_breaks_parquet,
    write_matches_parquet,
)
from backend.pipeline.normalize import CANONICAL_COLUMNS
from backend.pipeline.rules import (
    BREAK_DUPLICATE,
    BREAK_MISSING_BROKER,
    BREAK_MISSING_DESK,
    BREAK_PRICE,
    BREAK_QUANTITY,
    BREAK_SETTLEMENT,
    MATCH_PASS_CORPORATE_ACTION,
    MATCH_PASS_EXACT,
    MATCH_PASS_SPLIT_FILL,
    MATCH_PASS_TOLERANCE,
    empty_splits_frame,
    parse_trade_ids,
)


def _norm_row(
    *,
    source: str,
    trade_id: str,
    symbol: str = "AAPL",
    trade_date: str = "2024-06-03",
    settlement_date: str = "2024-06-04",
    side: str = "BUY",
    quantity: float = 100.0,
    price: float = 190.5,
    pair_id: str | None = "PAIR-001",
    **extra: Any,
) -> dict[str, Any]:
    row = {
        "trade_id": trade_id,
        "source": source,
        "symbol": symbol,
        "trade_date": trade_date,
        "settlement_date": settlement_date,
        "side": side,
        "quantity": quantity,
        "price": price,
        "currency": "USD",
        "account": "CLR-001" if source == "broker" else "EQ-ARB",
        "executing_party": "XNYS" if source == "broker" else "J.KIM",
        "pair_id": pair_id,
        "raw_payload": None,
    }
    row.update(extra)
    return row


def _frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(CANONICAL_COLUMNS))


def _splits_4for1(ticker: str = "AAPL", execution_date: str = "2024-06-10") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": ticker,
                "execution_date": execution_date,
                "split_from": 1,
                "split_to": 4,
            }
        ]
    )


def test_empty_matches_and_breaks_columns() -> None:
    assert list(empty_matches_frame().columns) == list(MATCH_COLUMNS)
    assert list(empty_breaks_frame().columns) == list(BREAK_COLUMNS)
    assert len(empty_matches_frame()) == 0
    assert len(empty_breaks_frame()) == 0


def test_split_legs_filters_source() -> None:
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1"),
            _norm_row(source="desk", trade_id="DSK-1"),
        ]
    )
    broker, desk = split_legs(df)
    assert list(broker["trade_id"]) == ["BRK-1"]
    assert list(desk["trade_id"]) == ["DSK-1"]


def test_split_legs_empty() -> None:
    broker, desk = split_legs(_frame([]))
    assert len(broker) == 0
    assert len(desk) == 0


def test_split_legs_missing_source_raises() -> None:
    df = pd.DataFrame({"trade_id": ["x"]})
    with pytest.raises(MatchError, match="source"):
        split_legs(df)


def test_match_trades_empty() -> None:
    result = match_trades(_frame([]))
    assert len(result.matches) == 0
    assert len(result.breaks) == 0
    assert result.summary["match_rows"] == 0


def test_match_normalized_trades_missing_columns_raises() -> None:
    with pytest.raises(MatchError, match="missing columns"):
        match_normalized_trades(pd.DataFrame({"trade_id": ["x"], "source": ["broker"]}))


def test_clean_exact_match() -> None:
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1", pair_id="PAIR-CLEAN"),
            _norm_row(source="desk", trade_id="DSK-1", pair_id="PAIR-CLEAN"),
        ]
    )
    matches, breaks = match_normalized_trades(df)
    assert len(matches) == 1
    assert len(breaks) == 0
    row = matches.iloc[0]
    assert row["match_pass"] == MATCH_PASS_EXACT
    assert row["broker_trade_id"] == "BRK-1"
    assert row["desk_trade_id"] == "DSK-1"
    assert row["pair_id"] == "PAIR-CLEAN"


def test_tolerance_price_match_not_exact() -> None:
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1", price=100.0),
            _norm_row(source="desk", trade_id="DSK-1", price=100.02),
        ]
    )
    matches, breaks = match_normalized_trades(df)
    assert len(matches) == 1
    assert matches.iloc[0]["match_pass"] == MATCH_PASS_TOLERANCE
    assert len(breaks) == 0


def test_price_break_outside_tolerance() -> None:
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1", price=100.0, pair_id="PAIR-PX"),
            _norm_row(source="desk", trade_id="DSK-1", price=100.75, pair_id="PAIR-PX"),
        ]
    )
    matches, breaks = match_normalized_trades(df)
    assert len(matches) == 0
    assert len(breaks) == 1
    br = breaks.iloc[0]
    assert br["break_type"] == BREAK_PRICE
    assert br["pair_id"] == "PAIR-PX"
    assert br["status"] == "open"
    assert parse_trade_ids(br["broker_trade_ids"]) == ["BRK-1"]
    assert parse_trade_ids(br["desk_trade_ids"]) == ["DSK-1"]
    ts = "2024-06-03T14:32:01-04:00"
    df = _frame(
        [
            _norm_row(
                source="broker",
                trade_id="BRK-1",
                price=100.0,
                pair_id="PAIR-PX",
                executed_at=ts,
            ),
            _norm_row(
                source="desk",
                trade_id="DSK-1",
                price=100.75,
                pair_id="PAIR-PX",
                executed_at=ts,
            ),
        ]
    )
    _matches, breaks = match_normalized_trades(df)
    executed = pd.to_datetime(breaks.iloc[0]["executed_at"], utc=True)
    assert executed.tzinfo is not None


def test_quantity_break() -> None:
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1", quantity=100.0),
            _norm_row(source="desk", trade_id="DSK-1", quantity=200.0),
        ]
    )
    _matches, breaks = match_normalized_trades(df)
    assert breaks.iloc[0]["break_type"] == BREAK_QUANTITY


def test_missing_desk() -> None:
    df = _frame([_norm_row(source="broker", trade_id="BRK-ONLY")])
    _matches, breaks = match_normalized_trades(df)
    assert breaks.iloc[0]["break_type"] == BREAK_MISSING_DESK
    assert breaks.iloc[0]["broker_trade_ids"] == "BRK-ONLY"
    assert breaks.iloc[0]["desk_trade_ids"] == ""


def test_missing_broker() -> None:
    df = _frame([_norm_row(source="desk", trade_id="DSK-ONLY")])
    _matches, breaks = match_normalized_trades(df)
    assert breaks.iloc[0]["break_type"] == BREAK_MISSING_BROKER
    assert breaks.iloc[0]["desk_trade_ids"] == "DSK-ONLY"


def test_duplicate_broker_booking() -> None:
    """Identical extra broker booking retracts the 1:1 match into a duplicate break."""
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1", pair_id="PAIR-DUP"),
            _norm_row(source="broker", trade_id="BRK-2", pair_id="PAIR-DUP"),
            _norm_row(source="desk", trade_id="DSK-1", pair_id="PAIR-DUP"),
        ]
    )
    matches, breaks = match_normalized_trades(df)
    assert len(matches) == 0
    assert len(breaks) == 1
    br = breaks.iloc[0]
    assert br["break_type"] == BREAK_DUPLICATE
    assert set(parse_trade_ids(br["broker_trade_ids"])) == {"BRK-1", "BRK-2"}
    assert parse_trade_ids(br["desk_trade_ids"]) == ["DSK-1"]


def test_settlement_date_mismatch() -> None:
    df = _frame(
        [
            _norm_row(
                source="broker",
                trade_id="BRK-1",
                settlement_date="2024-06-04",
            ),
            _norm_row(
                source="desk",
                trade_id="DSK-1",
                settlement_date="2024-06-05",
            ),
        ]
    )
    matches, breaks = match_normalized_trades(df)
    assert len(matches) == 0
    assert breaks.iloc[0]["break_type"] == BREAK_SETTLEMENT


def test_split_fill_one_desk_many_broker() -> None:
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-A", quantity=40.0, pair_id="PAIR-SF"),
            _norm_row(source="broker", trade_id="BRK-B", quantity=60.0, pair_id="PAIR-SF"),
            _norm_row(source="desk", trade_id="DSK-1", quantity=100.0, pair_id="PAIR-SF"),
        ]
    )
    matches, breaks = match_normalized_trades(df)
    assert len(breaks) == 0
    assert len(matches) == 2
    assert set(matches["match_pass"]) == {MATCH_PASS_SPLIT_FILL}
    assert set(matches["broker_trade_id"]) == {"BRK-A", "BRK-B"}
    assert set(matches["desk_trade_id"]) == {"DSK-1"}


def test_corporate_action_not_flagged_as_break() -> None:
    df = _frame(
        [
            _norm_row(
                source="broker",
                trade_id="BRK-CA",
                quantity=400.0,
                price=50.0,
                trade_date="2024-06-09",
                pair_id="PAIR-CA",
            ),
            _norm_row(
                source="desk",
                trade_id="DSK-CA",
                quantity=100.0,
                price=200.0,
                trade_date="2024-06-09",
                pair_id="PAIR-CA",
            ),
        ]
    )
    matches, breaks = match_normalized_trades(df, splits=_splits_4for1())
    assert len(breaks) == 0
    assert len(matches) == 1
    assert matches.iloc[0]["match_pass"] == MATCH_PASS_CORPORATE_ACTION


def test_corporate_action_without_splits_is_quantity_break() -> None:
    df = _frame(
        [
            _norm_row(
                source="broker",
                trade_id="BRK-CA",
                quantity=400.0,
                price=50.0,
                trade_date="2024-06-09",
            ),
            _norm_row(
                source="desk",
                trade_id="DSK-CA",
                quantity=100.0,
                price=200.0,
                trade_date="2024-06-09",
            ),
        ]
    )
    matches, breaks = match_normalized_trades(df, splits=empty_splits_frame())
    assert len(matches) == 0
    assert breaks.iloc[0]["break_type"] == BREAK_QUANTITY


def test_pair_id_is_not_a_matching_key() -> None:
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1", pair_id="PAIR-A"),
            _norm_row(source="desk", trade_id="DSK-1", pair_id="PAIR-B"),
        ]
    )
    matches, _breaks = match_normalized_trades(df)
    assert len(matches) == 1
    assert matches.iloc[0]["match_pass"] == MATCH_PASS_EXACT


def test_mixed_break_types_in_one_frame() -> None:
    df = _frame(
        [
            _norm_row(
                source="broker",
                trade_id="BRK-CL",
                symbol="MSFT",
                pair_id="P-CL",
            ),
            _norm_row(
                source="desk",
                trade_id="DSK-CL",
                symbol="MSFT",
                pair_id="P-CL",
            ),
            _norm_row(
                source="broker",
                trade_id="BRK-MISS",
                symbol="NVDA",
                pair_id="P-MISS",
            ),
        ]
    )
    matches, breaks = match_normalized_trades(df)
    assert len(matches) == 1
    assert matches.iloc[0]["match_pass"] == MATCH_PASS_EXACT
    assert len(breaks) == 1
    assert breaks.iloc[0]["break_type"] == BREAK_MISSING_DESK


def test_configurable_qty_tolerance() -> None:
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1", quantity=100.0),
            _norm_row(source="desk", trade_id="DSK-1", quantity=100.4),
        ]
    )
    _m_tight, b_tight = match_normalized_trades(df, qty_abs_tol=1e-6)
    assert b_tight.iloc[0]["break_type"] == BREAK_QUANTITY
    m_loose, b_loose = match_normalized_trades(df, qty_abs_tol=1.0)
    assert len(b_loose) == 0
    assert m_loose.iloc[0]["match_pass"] in {
        MATCH_PASS_EXACT,
        MATCH_PASS_TOLERANCE,
    }


def test_summarize_match_frames() -> None:
    matches, breaks = match_normalized_trades(
        _frame(
            [
                _norm_row(source="broker", trade_id="BRK-1"),
                _norm_row(source="desk", trade_id="DSK-1"),
            ]
        )
    )
    summary = summarize_match_frames(matches, breaks)
    assert summary["match_rows"] == 1
    assert summary["match_pass_counts"][MATCH_PASS_EXACT] == 1


def test_resolve_match_paths_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NORMALIZED_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("MATCHED_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("MARKET_DATA_CACHE_DIR", raising=False)
    paths = resolve_match_paths()
    assert paths.normalized.name == "normalized_trades.parquet"
    assert paths.matches.name == "matches.parquet"
    assert paths.breaks.name == "breaks.parquet"


def test_default_matched_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MATCHED_OUTPUT_DIR", str(tmp_path / "m"))
    assert default_matched_output_dir() == tmp_path / "m"


def test_resolve_match_paths_overrides(tmp_path: Path) -> None:
    paths = resolve_match_paths(
        normalized_dir=tmp_path / "n",
        output_dir=tmp_path / "o",
        cache_dir=tmp_path / "c",
    )
    assert paths.normalized == tmp_path / "n" / "normalized_trades.parquet"
    assert paths.matches == tmp_path / "o" / "matches.parquet"
    assert paths.splits == tmp_path / "c" / "splits.parquet"


def test_parquet_round_trip(tmp_path: Path) -> None:
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1"),
            _norm_row(source="desk", trade_id="DSK-1"),
        ]
    )
    matches, breaks = match_normalized_trades(df)
    m_path = write_matches_parquet(matches, tmp_path / "matches.parquet")
    b_path = write_breaks_parquet(breaks, tmp_path / "breaks.parquet")
    loaded_m = pd.read_parquet(m_path)
    loaded_b = pd.read_parquet(b_path)
    assert "match_id" in loaded_m.columns
    assert list(MATCH_COLUMNS) == [c for c in loaded_m.columns if c != "match_id"]
    assert "break_id" in loaded_b.columns
    assert len(loaded_m) == 1
    assert len(loaded_b) == 0


def test_prepare_frames_for_parquet() -> None:
    prepared_m = prepare_matches_for_parquet(empty_matches_frame())
    assert "match_id" in prepared_m.columns
    breaks = pd.DataFrame(
        [
            {
                "break_type": BREAK_PRICE,
                "status": "open",
                "pair_id": "P",
                "broker_trade_ids": "BRK-1",
                "desk_trade_ids": "DSK-1",
                "symbol": "AAPL",
                "trade_date": date(2024, 6, 3),
                "detail": {"price_diff_bps": 75.0},
            }
        ]
    )
    prepared_b = prepare_breaks_for_parquet(breaks)
    assert isinstance(prepared_b.iloc[0]["detail"], str)
    assert json.loads(prepared_b.iloc[0]["detail"])["price_diff_bps"] == 75.0
    assert prepared_b.iloc[0]["trade_date"] == "2024-06-03"
    first_id = prepared_b.iloc[0]["break_id"]
    again = prepare_breaks_for_parquet(breaks)
    assert again.iloc[0]["break_id"] == first_id
    UUID(str(first_id))
    assert stable_break_id(breaks.iloc[0].to_dict()) == UUID(str(first_id))
    assert "pair_id" in breaks.iloc[0].to_dict()
    assert "|P|" in break_identity(breaks.iloc[0].to_dict())


def test_load_frames_to_db_does_not_delete_audit_log() -> None:
    import inspect

    src = inspect.getsource(matcher_mod.load_frames_to_db)
    assert "delete(AuditLog)" not in src
    assert "delete(Match)" in src


def test_read_normalized_parquet(tmp_path: Path) -> None:
    path = tmp_path / "normalized_trades.parquet"
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1"),
            _norm_row(source="desk", trade_id="DSK-1"),
        ]
    )
    df.to_parquet(path, index=False)
    loaded = read_normalized_parquet(path)
    assert len(loaded) == 2
    with pytest.raises(FileNotFoundError, match="normalized"):
        read_normalized_parquet(tmp_path / "missing.parquet")


def test_load_splits_cache_missing_and_present(tmp_path: Path) -> None:
    missing = load_splits_cache(tmp_path)
    assert len(missing) == 0
    _splits_4for1().to_parquet(tmp_path / "splits.parquet", index=False)
    loaded = load_splits_cache(tmp_path)
    assert len(loaded) == 1


def test_orm_row_converters() -> None:
    match = match_row_to_orm(
        {
            "match_id": str(uuid4()),
            "broker_trade_id": "BRK-1",
            "desk_trade_id": "DSK-1",
            "pair_id": "PAIR-1",
            "match_pass": MATCH_PASS_EXACT,
        }
    )
    assert match.broker_trade_id == "BRK-1"
    assert match.match_pass == MATCH_PASS_EXACT
    brk = break_row_to_orm(
        {
            "break_id": str(uuid4()),
            "break_type": BREAK_PRICE,
            "status": "open",
            "pair_id": "PAIR-1",
            "broker_trade_ids": "BRK-1",
            "desk_trade_ids": "DSK-1",
            "symbol": "AAPL",
            "trade_date": "2024-06-03",
            "detail": json.dumps({"price_diff_bps": 75}),
            "cluster_id": None,
        }
    )
    assert brk.break_type == BREAK_PRICE
    assert brk.detail == {"price_diff_bps": 75}
    assert brk.trade_date == date(2024, 6, 3)


def test_normalized_orm_to_record() -> None:
    orm = NormalizedTrade(
        trade_id="BRK-1",
        source="broker",
        symbol="AAPL",
        trade_date=date(2024, 6, 3),
        settlement_date=date(2024, 6, 4),
        side="BUY",
        quantity=100.0,
        price=190.5,
        currency="USD",
        account="CLR-001",
        executing_party="XNYS",
        pair_id="PAIR-1",
        raw_payload={"k": "v"},
    )
    rec = normalized_orm_to_record(orm)
    assert rec["trade_id"] == "BRK-1"
    assert rec["source"] == "broker"
    assert rec["pair_id"] == "PAIR-1"


def test_match_row_to_orm_uuid_instance() -> None:
    mid = uuid4()
    orm = match_row_to_orm(
        {
            "match_id": mid,
            "broker_trade_id": "B",
            "desk_trade_id": "D",
            "pair_id": None,
            "match_pass": MATCH_PASS_EXACT,
        }
    )
    assert orm.match_id == mid
    assert isinstance(orm.match_id, UUID)


def test_break_row_to_orm_with_cluster() -> None:
    cid = uuid4()
    orm = break_row_to_orm(
        {
            "break_id": str(uuid4()),
            "break_type": BREAK_DUPLICATE,
            "status": "open",
            "pair_id": float("nan"),
            "broker_trade_ids": float("nan"),
            "desk_trade_ids": float("nan"),
            "symbol": "AAPL",
            "trade_date": None,
            "detail": {"extra_source": "broker"},
            "cluster_id": cid,
        }
    )
    assert orm.cluster_id == cid
    assert orm.pair_id is None


def test_run_match_writes_parquet(tmp_path: Path) -> None:
    norm_dir = tmp_path / "normalized"
    out_dir = tmp_path / "matched"
    cache_dir = tmp_path / "cache"
    norm_dir.mkdir()
    cache_dir.mkdir()
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1"),
            _norm_row(source="desk", trade_id="DSK-1"),
        ]
    )
    df.to_parquet(norm_dir / "normalized_trades.parquet", index=False)
    result = run_match(
        normalized_dir=norm_dir,
        output_dir=out_dir,
        cache_dir=cache_dir,
        load_db=False,
    )
    assert result.match_rows == 1
    assert result.break_rows == 0
    assert result.db_loaded is False
    assert result.source == "parquet"
    assert result.matches_path.exists()
    assert result.breaks_path.exists()


def test_run_match_missing_input(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_match(
            normalized_dir=tmp_path,
            output_dir=tmp_path / "out",
            cache_dir=tmp_path,
            load_db=False,
        )


def test_run_match_load_db_requires_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    norm_dir = tmp_path / "n"
    norm_dir.mkdir()
    _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1"),
            _norm_row(source="desk", trade_id="DSK-1"),
        ]
    ).to_parquet(norm_dir / "normalized_trades.parquet", index=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        run_match(
            normalized_dir=norm_dir,
            output_dir=tmp_path / "o",
            cache_dir=tmp_path,
            load_db=True,
        )


def test_build_arg_parser() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--no-db", "--price-tolerance-bps", "10"])
    assert args.no_db is True
    assert args.price_tolerance_bps == 10.0


def test_main_cli_parquet_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    norm_dir = tmp_path / "n"
    out_dir = tmp_path / "o"
    cache_dir = tmp_path / "c"
    norm_dir.mkdir()
    cache_dir.mkdir()
    _frame(
        [
            _norm_row(source="broker", trade_id="BRK-1"),
            _norm_row(source="desk", trade_id="DSK-1"),
        ]
    ).to_parquet(norm_dir / "normalized_trades.parquet", index=False)
    code = matcher_mod.main(
        [
            "--input-dir",
            str(norm_dir),
            "--output-dir",
            str(out_dir),
            "--cache-dir",
            str(cache_dir),
            "--no-db",
        ]
    )
    assert code == 0
    assert (out_dir / "matches.parquet").exists()


def test_main_cli_missing_input(tmp_path: Path) -> None:
    code = matcher_mod.main(
        [
            "--input-dir",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "o"),
            "--cache-dir",
            str(tmp_path),
            "--no-db",
        ]
    )
    assert code == 1


def test_synthetic_generator_ca_is_match_not_break() -> None:
    from backend.data.fetch_market_data import (
        BARS_COLUMNS,
        CALENDAR_COLUMNS,
        SPLITS_COLUMNS,
    )
    from backend.data.generator import (
        BREAK_CLEAN,
        BREAK_CORPORATE_ACTION,
        BreakRates,
        GeneratorConfig,
        MarketCache,
        generate_trades,
    )
    from backend.pipeline.normalize import normalize_both

    bars = pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "date": "2024-06-03",
                "open": 193.0,
                "high": 196.0,
                "low": 192.0,
                "close": 194.0,
                "volume": 1_000_000,
                "vwap": 194.0,
                "transactions": 5000,
            }
        ],
        columns=list(BARS_COLUMNS),
    )
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    splits = pd.DataFrame(
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
        ],
        columns=list(SPLITS_COLUMNS),
    )
    splits["execution_date"] = pd.to_datetime(splits["execution_date"]).dt.date
    calendar = pd.DataFrame(columns=list(CALENDAR_COLUMNS))
    cache = MarketCache(
        bars=bars, splits=splits, calendar=calendar, cache_dir=Path(".")
    )
    cfg = GeneratorConfig(
        n_trades=20,
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
        max_corporate_action_breaks=1,
    )
    gen = generate_trades(cache, cfg)
    normalized = normalize_both(gen.broker, gen.desk, include_raw_payload=False)
    matches, breaks = match_normalized_trades(normalized, splits=splits)
    ca_pairs = set(
        gen.ground_truth.loc[
            gen.ground_truth["break_type"] == BREAK_CORPORATE_ACTION, "pair_id"
        ]
    )
    clean_pairs = set(
        gen.ground_truth.loc[
            gen.ground_truth["break_type"] == BREAK_CLEAN, "pair_id"
        ]
    )
    assert ca_pairs, "generator should emit a CA pair"
    matched_pairs = set(matches["pair_id"].dropna())
    broken_pairs = set(breaks["pair_id"].dropna()) if len(breaks) else set()
    assert ca_pairs <= matched_pairs
    assert ca_pairs.isdisjoint(broken_pairs)
    assert set(
        matches.loc[matches["pair_id"].isin(ca_pairs), "match_pass"]
    ) == {MATCH_PASS_CORPORATE_ACTION}
    assert clean_pairs <= matched_pairs


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").strip(),
    reason="DATABASE_URL not set — optional Postgres integration",
)
def test_load_frames_to_db_integration() -> None:
    from sqlalchemy import func, select

    from backend.db.models import Break, Match
    from backend.db.session import (
        create_all_tables,
        database_url_from_env,
        get_engine,
        get_session_factory,
        session_scope,
    )
    from backend.pipeline.matcher import load_frames_to_db, load_to_database, read_normalized_from_db

    url = database_url_from_env()
    assert url is not None
    df = _frame(
        [
            _norm_row(source="broker", trade_id="BRK-IT"),
            _norm_row(source="desk", trade_id="DSK-IT"),
        ]
    )
    matches, breaks = match_normalized_trades(df)
    engine = get_engine(url)
    create_all_tables(engine)
    factory = get_session_factory(engine)
    with session_scope(factory) as session:
        counts = load_frames_to_db(matches, breaks, session, replace=True)
        assert counts["matches"] == 1
        n_match = session.scalar(select(func.count()).select_from(Match))
        n_break = session.scalar(select(func.count()).select_from(Break))
        assert n_match == 1
        assert n_break == 0
    db_counts = load_to_database(matches, breaks, url, replace=True)
    assert db_counts["matches"] == 1
    loaded = read_normalized_from_db(url)
    assert "trade_id" in loaded.columns
