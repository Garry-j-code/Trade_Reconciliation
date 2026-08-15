"""Unit tests for recon orchestration (Parquet I/O; DB optional)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from backend.data.generator import BROKER_COLUMNS, DESK_COLUMNS
from backend.pipeline.matcher import MATCH_PASS_EXACT, match_normalized_trades
from backend.pipeline.normalize import CANONICAL_COLUMNS, normalize_both
from backend.pipeline.recon import (
    DEFAULT_RECON_TIMEOUT_SECONDS,
    ReconTimeoutError,
    breaks_to_orm,
    matches_to_orm,
    recon_timeout_seconds,
    run_recon,
    run_recon_capped,
    run_rematch_from_db,
)
from backend.pipeline.rules import BREAK_PRICE


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


def test_recon_timeout_seconds_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECON_TIMEOUT_SECONDS", raising=False)
    assert recon_timeout_seconds() == DEFAULT_RECON_TIMEOUT_SECONDS


def test_recon_timeout_seconds_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECON_TIMEOUT_SECONDS", "30")
    assert recon_timeout_seconds() == 30.0
    assert recon_timeout_seconds({"RECON_TIMEOUT_SECONDS": "0.5"}) == 1.0


def test_matches_to_orm_and_breaks_to_orm() -> None:
    broker = pd.DataFrame(_broker_rows(), columns=list(BROKER_COLUMNS))
    desk = pd.DataFrame(_desk_rows(), columns=list(DESK_COLUMNS))
    normalized = normalize_both(broker, desk)
    matches, breaks = match_normalized_trades(normalized)
    orms = matches_to_orm(matches)
    assert len(orms) == 1
    assert orms[0].match_pass == MATCH_PASS_EXACT
    assert orms[0].broker_trade_id == "BRK-1"
    assert matches_to_orm(matches.iloc[0:0]) == []

    price_break = pd.DataFrame(
        [
            {
                "break_type": BREAK_PRICE,
                "status": "open",
                "pair_id": "PAIR-X",
                "broker_trade_ids": "BRK-1",
                "desk_trade_ids": "DSK-1",
                "symbol": "MSFT",
                "trade_date": "2024-06-03",
                "detail": {"notional_at_risk": 20000.0},
            }
        ]
    )
    b_orms = breaks_to_orm(price_break)
    assert len(b_orms) == 1
    assert b_orms[0].break_type == BREAK_PRICE
    assert b_orms[0].detail == {"notional_at_risk": 20000.0}
    assert breaks_to_orm(price_break.iloc[0:0]) == []


def test_run_recon_writes_matched_parquet(
    generated_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out = tmp_path / "normalized"
    matched = tmp_path / "matched"
    result = run_recon(
        input_dir=generated_dir,
        output_dir=out,
        cache_dir=tmp_path / "cache",
        matched_output_dir=matched,
        load_db=False,
    )
    assert result.match_count == 1
    assert result.break_count == 0
    assert result.db_loaded is False
    assert result.parquet_path is not None and result.parquet_path.exists()
    assert result.matches_path is not None and result.matches_path.exists()
    assert result.breaks_path is not None and result.breaks_path.exists()
    loaded = pd.read_parquet(result.parquet_path)
    assert list(loaded.columns) == list(CANONICAL_COLUMNS)


def test_run_recon_capped(
    generated_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = run_recon_capped(
        timeout_seconds=60.0,
        input_dir=generated_dir,
        output_dir=tmp_path / "n",
        cache_dir=tmp_path / "c",
        matched_output_dir=tmp_path / "m",
        load_db=False,
    )
    assert result.match_count == 1


def test_run_recon_capped_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _slow(**_kwargs: Any) -> None:
        import time

        time.sleep(2)

    monkeypatch.setattr("backend.pipeline.recon.run_recon", _slow)
    with pytest.raises(ReconTimeoutError, match="cap"):
        run_recon_capped(timeout_seconds=0.05)


def test_run_recon_load_db_requires_url(
    generated_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        run_recon(
            input_dir=generated_dir,
            output_dir=tmp_path / "n",
            cache_dir=tmp_path / "c",
            matched_output_dir=tmp_path / "m",
            load_db=True,
        )


def test_run_rematch_from_db_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        run_rematch_from_db()


def test_run_rematch_from_db_empty_book(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.pipeline.recon.read_normalized_from_db",
        lambda _url: pd.DataFrame(),
    )
    with pytest.raises(ValueError, match="No normalized trades"):
        run_rematch_from_db(
            database_url="postgresql://example.invalid/trade_recon"
        )


def test_run_rematch_from_db_skips_parquet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = pd.DataFrame(
        {
            "source": ["broker", "desk"],
            "trade_id": ["B1", "D1"],
        }
    )
    monkeypatch.setattr(
        "backend.pipeline.recon.read_normalized_from_db",
        lambda _url: book,
    )

    class _Match:
        match_rows = 1
        break_rows = 0
        summary = {"break_type_counts": {}}
        db_loaded = True

    captured: dict[str, Any] = {}

    def _match(**kwargs: Any) -> _Match:
        captured.update(kwargs)
        return _Match()

    monkeypatch.setattr("backend.pipeline.recon.run_match", _match)
    result = run_rematch_from_db(
        database_url="postgresql://example.invalid/trade_recon"
    )
    assert result.normalized_rows == 2
    assert result.broker_rows == 1
    assert result.desk_rows == 1
    assert result.match_count == 1
    assert captured["from_db"] is True
    assert captured["write_parquet"] is False
    assert captured["load_db"] is True
