"""Daily blotter: dated generate + one-day ingest without wiping other dates."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from backend.data.generator import GeneratorConfig, generate_trades, load_market_cache
from backend.pipeline.daily_blotter import run_daily_blotter
from backend.pipeline.ingest import merge_parquet_by_trade_date
from backend.tests.test_generator import build_tiny_cache


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


def test_delete_generated_trade_files(tmp_path: Path) -> None:
    from backend.data.generator import delete_generated_trade_files

    out = tmp_path / "generated"
    out.mkdir()
    (out / "broker_trades.parquet").write_bytes(b"x")
    (out / "desk_trades.parquet").write_bytes(b"y")
    (out / "generation_summary.json").write_text("{}", encoding="utf-8")
    (out / "keep.txt").write_text("stay", encoding="utf-8")
    removed = delete_generated_trade_files(out)
    assert len(removed) == 3
    assert not (out / "broker_trades.parquet").exists()
    assert (out / "keep.txt").exists()


def test_daily_blotter_keeps_generated_when_db_not_loaded(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.data.generator import should_delete_generated_after_db

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("S3_CACHE_BUCKET", raising=False)
    monkeypatch.setenv("TRADE_RECON_DELETE_GENERATED", "1")
    monkeypatch.setenv("TRADE_OUTPUT_DIR", str(tmp_path / "generated"))
    monkeypatch.setenv("NORMALIZED_OUTPUT_DIR", str(tmp_path / "normalized"))
    monkeypatch.setenv("MATCHED_OUTPUT_DIR", str(tmp_path / "matched"))
    assert should_delete_generated_after_db() is True
    run_daily_blotter(
        trade_date=date(2024, 6, 3),
        skip_fetch=True,
        skip_s3_sync=True,
        cache_dir=cache_dir,
        n_trades=8,
        load_db=False,
        write_parquet=True,
    )
    assert (tmp_path / "generated" / "broker_trades.parquet").exists()


def test_daily_blotter_deletes_generated_after_db_load(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen_dir = tmp_path / "generated"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("S3_CACHE_BUCKET", raising=False)
    monkeypatch.setenv("TRADE_RECON_DELETE_GENERATED", "1")
    monkeypatch.setenv("TRADE_OUTPUT_DIR", str(gen_dir))
    monkeypatch.setenv("NORMALIZED_OUTPUT_DIR", str(tmp_path / "normalized"))
    monkeypatch.setenv("MATCHED_OUTPUT_DIR", str(tmp_path / "matched"))

    def _session(*_args: object, **_kwargs: object) -> dict[str, object]:
        gen_dir.mkdir(parents=True, exist_ok=True)
        (gen_dir / "broker_trades.parquet").write_bytes(b"x")
        return {
            "skipped": False,
            "trade_date": "2024-06-03",
            "generate": {"n_broker_rows": 1, "n_desk_rows": 1},
            "normalized_rows": 2,
            "db_loaded": True,
            "parquet_dir": str(tmp_path / "normalized"),
        }

    class _Match:
        match_rows = 0
        break_rows = 0
        db_loaded = True

    monkeypatch.setattr(
        "backend.pipeline.daily_blotter.run_one_session", _session
    )
    monkeypatch.setattr(
        "backend.pipeline.daily_blotter.run_match",
        lambda **_kwargs: _Match(),
    )
    monkeypatch.setattr(
        "backend.pipeline.daily_blotter.load_market_cache",
        lambda _cache: load_market_cache(cache_dir),
    )
    result = run_daily_blotter(
        trade_date=date(2024, 6, 3),
        skip_fetch=True,
        skip_s3_sync=True,
        cache_dir=cache_dir,
        n_trades=8,
        load_db=True,
        write_parquet=True,
    )
    assert result.db_loaded is True
    assert not (gen_dir / "broker_trades.parquet").exists()
    assert any(n.startswith("deleted_generated=") for n in result.notes)
