"""Daily blotter: dated generate + one-day ingest without wiping other dates."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from backend.data.generator import GeneratorConfig, generate_trades, load_market_cache
from backend.pipeline.daily_blotter import run_daily_blotter
from backend.pipeline.ingest import merge_parquet_by_trade_date
from backend.tests.test_generator import tiny_cache as build_tiny_cache


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return build_tiny_cache(tmp_path)


def test_daily_blotter_two_dates_no_duplicate_ids(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("S3_CACHE_BUCKET", raising=False)
    monkeypatch.setenv("TRADE_OUTPUT_DIR", str(tmp_path / "generated"))
    monkeypatch.setenv("NORMALIZED_OUTPUT_DIR", str(tmp_path / "normalized"))
    monkeypatch.setenv("MATCHED_OUTPUT_DIR", str(tmp_path / "matched"))

    first = run_daily_blotter(
        trade_date=date(2024, 6, 3),
        skip_fetch=True,
        skip_s3_sync=True,
        cache_dir=cache_dir,
        n_trades=8,
        load_db=False,
        write_parquet=True,
    )
    assert first.skipped == []
    assert first.match_count + first.break_count >= 0

    second = run_daily_blotter(
        trade_date=date(2024, 5, 31),
        skip_fetch=True,
        skip_s3_sync=True,
        cache_dir=cache_dir,
        n_trades=8,
        load_db=False,
        write_parquet=True,
    )
    assert second.skipped == []
    norm = pd.read_parquet(tmp_path / "normalized" / "normalized_trades.parquet")
    dates = set(pd.to_datetime(norm["trade_date"]).dt.date)
    assert date(2024, 6, 3) in dates
    assert date(2024, 5, 31) in dates

    again = run_daily_blotter(
        trade_date=date(2024, 6, 3),
        skip_fetch=True,
        skip_s3_sync=True,
        cache_dir=cache_dir,
        n_trades=8,
        load_db=False,
        write_parquet=True,
    )
    assert again.skipped == []
    rerun = pd.read_parquet(tmp_path / "normalized" / "normalized_trades.parquet")
    june = rerun[pd.to_datetime(rerun["trade_date"]).dt.date == date(2024, 6, 3)]
    first_june = norm[pd.to_datetime(norm["trade_date"]).dt.date == date(2024, 6, 3)]
    assert sorted(june["trade_id"].tolist()) == sorted(first_june["trade_id"].tolist())
    may = rerun[pd.to_datetime(rerun["trade_date"]).dt.date == date(2024, 5, 31)]
    assert len(may) > 0


def test_same_date_generate_same_trade_ids(cache_dir: Path) -> None:
    cache = load_market_cache(cache_dir)
    cfg = GeneratorConfig(
        cache_dir=cache_dir,
        n_trades=10,
        seed=42,
        trade_date=date(2024, 6, 3),
        max_corporate_action_breaks=0,
    )
    a = generate_trades(cache, cfg)
    b = generate_trades(cache, cfg)
    assert list(a.broker["broker_trade_id"]) == list(b.broker["broker_trade_id"])
    merged = merge_parquet_by_trade_date(a.broker, b.broker, date(2024, 6, 3))
    assert len(merged) == len(b.broker)
