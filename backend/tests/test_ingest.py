"""Unit tests for ingest load path (Parquet I/O; DB optional)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from backend.data.generator import BROKER_COLUMNS, DESK_COLUMNS
from backend.pipeline import ingest
from backend.pipeline.ingest import (
    filter_frame_to_trade_date,
    load_frames_to_db,
    merge_parquet_by_trade_date,
    read_generated_trades,
    resolve_paths,
    run_normalize,
    write_normalized_parquet,
)
from backend.pipeline.normalize import CANONICAL_COLUMNS, normalize_both


def _broker_rows() -> list[dict[str, Any]]:
    return [
        {
            "broker_trade_id": "BRK-1",
            "symbol": "MSFT",
            "trade_date": "2024-06-03",
            "settlement_date": "2024-06-04",
            "side": "BUY",
            "quantity": 50.0,
            "price": 400.0,
            "currency": "USD",
            "account_id": "CLR-001",
            "execution_venue": "XNYS",
            "pair_id": "PAIR-X",
        }
    ]


def _desk_rows() -> list[dict[str, Any]]:
    return [
        {
            "blotter_id": "DSK-1",
            "ticker": "MSFT",
            "trade_date": "2024-06-03",
            "settle_date": "2024-06-04",
            "side": "BUY",
            "qty": 50.0,
            "px": 400.0,
            "ccy": "USD",
            "desk_code": "EQ-INDEX",
            "trader": "S.MORALES",
            "pair_id": "PAIR-X",
        }
    ]


@pytest.fixture
def generated_dir(tmp_path: Path) -> Path:
    inp = tmp_path / "generated"
    inp.mkdir()
    pd.DataFrame(_broker_rows(), columns=list(BROKER_COLUMNS)).to_parquet(
        inp / "broker_trades.parquet", index=False
    )
    pd.DataFrame(_desk_rows(), columns=list(DESK_COLUMNS)).to_parquet(
        inp / "desk_trades.parquet", index=False
    )
    return inp


def test_resolve_paths_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADE_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("NORMALIZED_OUTPUT_DIR", raising=False)
    paths = resolve_paths()
    assert paths.broker.name == "broker_trades.parquet"
    assert paths.normalized.name == "normalized_trades.parquet"


def test_read_generated_trades(generated_dir: Path) -> None:
    broker, desk = read_generated_trades(generated_dir)
    assert len(broker) == 1
    assert len(desk) == 1
    assert broker.iloc[0]["broker_trade_id"] == "BRK-1"


def test_read_generated_trades_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="broker"):
        read_generated_trades(tmp_path)


def test_run_normalize_writes_parquet(
    generated_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "normalized"
    result = run_normalize(
        input_dir=generated_dir,
        output_dir=out,
        load_db=False,
    )
    assert result.normalized_rows == 2
    assert result.parquet_path.exists()
    assert result.db_loaded is False
    df = pd.read_parquet(result.parquet_path)
    assert list(df.columns) == list(CANONICAL_COLUMNS)
    assert set(df["source"]) == {"broker", "desk"}
    assert set(df["trade_id"]) == {"BRK-1", "DSK-1"}
    assert set(df["pair_id"]) == {"PAIR-X"}


def test_write_normalized_parquet_round_trip(tmp_path: Path) -> None:
    broker = pd.DataFrame(_broker_rows(), columns=list(BROKER_COLUMNS))
    desk = pd.DataFrame(_desk_rows(), columns=list(DESK_COLUMNS))
    normalized = normalize_both(broker, desk)
    path = write_normalized_parquet(normalized, tmp_path / "n.parquet")
    loaded = pd.read_parquet(path)
    assert len(loaded) == 2
    payload = loaded.loc[loaded["source"] == "broker", "raw_payload"].iloc[0]
    assert isinstance(payload, str)
    assert json.loads(payload)["broker_trade_id"] == "BRK-1"


def test_main_cli_parquet_only(
    generated_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out = tmp_path / "out"
    code = ingest.main(
        [
            "--input-dir",
            str(generated_dir),
            "--output-dir",
            str(out),
            "--no-db",
        ]
    )
    assert code == 0
    assert (out / "normalized_trades.parquet").exists()


def test_main_cli_missing_input(tmp_path: Path) -> None:
    code = ingest.main(
        ["--input-dir", str(tmp_path), "--output-dir", str(tmp_path / "o"), "--no-db"]
    )
    assert code == 1


def test_merge_parquet_by_trade_date_replaces_one_day() -> None:
    from datetime import date

    older = pd.DataFrame(
        {
            "trade_id": ["A", "B"],
            "trade_date": ["2024-06-03", "2024-06-04"],
        }
    )
    incoming = pd.DataFrame(
        {"trade_id": ["C"], "trade_date": ["2024-06-03"]}
    )
    merged = merge_parquet_by_trade_date(older, incoming, date(2024, 6, 3))
    ids = set(merged["trade_id"])
    assert ids == {"B", "C"}
    filtered = filter_frame_to_trade_date(older, date(2024, 6, 4))
    assert list(filtered["trade_id"]) == ["B"]


def test_run_normalize_trade_date_does_not_drop_other_parquet_days(
    generated_dir: Path, tmp_path: Path
) -> None:
    from datetime import date

    out = tmp_path / "normalized"
    first = run_normalize(input_dir=generated_dir, output_dir=out, load_db=False)
    assert first.normalized_rows == 2
    extra = pd.read_parquet(first.parquet_path)
    extra.loc[extra["trade_id"] == "BRK-1", "trade_id"] = "BRK-OLD"
    extra.loc[extra["trade_id"] == "DSK-1", "trade_id"] = "DSK-OLD"
    extra["trade_date"] = "2024-06-04"
    extra.to_parquet(first.parquet_path, index=False)

    second = run_normalize(
        input_dir=generated_dir,
        output_dir=out,
        load_db=False,
        trade_date=date(2024, 6, 3),
    )
    loaded = pd.read_parquet(second.parquet_path)
    dates = set(pd.to_datetime(loaded["trade_date"]).dt.date.astype(str))
    assert "2024-06-03" in dates
    assert "2024-06-04" in dates
    assert "BRK-OLD" in set(loaded["trade_id"])
    assert "BRK-1" in set(loaded["trade_id"])


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").strip(),
    reason="DATABASE_URL not set — optional Postgres integration",
)
def test_load_frames_to_db_integration(generated_dir: Path) -> None:
    from sqlalchemy import func, select

    from backend.db.models import NormalizedTrade, RawBrokerTrade, RawDeskTrade
    from backend.db.session import (
        create_all_tables,
        database_url_from_env,
        get_engine,
        get_session_factory,
        session_scope,
    )

    url = database_url_from_env()
    assert url is not None
    broker, desk = read_generated_trades(generated_dir)
    normalized = normalize_both(broker, desk)

    engine = get_engine(url)
    create_all_tables(engine)
    factory = get_session_factory(engine)
    with session_scope(factory) as session:
        counts = load_frames_to_db(broker, desk, normalized, session, replace=True)
        assert counts["normalized"] == 2
        n_broker = session.scalar(select(func.count()).select_from(RawBrokerTrade))
        n_desk = session.scalar(select(func.count()).select_from(RawDeskTrade))
        n_norm = session.scalar(select(func.count()).select_from(NormalizedTrade))
        assert n_broker == 1
        assert n_desk == 1
        assert n_norm == 2
