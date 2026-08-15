"""Auto-investigate after match — agent layer, not pipeline matching."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
    SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]

from backend.agent.auto_investigate import (
    investigate_missing_suggestions,
    open_breaks_without_suggestions,
)
from backend.agent.providers import StubProvider
from backend.db.models import Base, Break, ResolutionSuggestion
from backend.pipeline.daily_blotter import run_daily_blotter


def test_pipeline_daily_blotter_module_does_not_import_agent() -> None:
    import inspect

    from backend.pipeline import daily_blotter as blotter

    src = inspect.getsource(blotter)
    assert "backend.agent" not in src
    assert "investigate_break" not in src


def test_investigate_missing_writes_suggestion_and_skips_existing(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(
        engine,
        tables=[Break.__table__, ResolutionSuggestion.__table__],
    )
    session = sessionmaker(bind=engine, future=True)()
    missing = Break(
        break_id=uuid4(),
        break_type="price_break",
        status="open",
        symbol="AAPL",
        trade_date=date(2024, 6, 3),
        created_at=datetime.now(timezone.utc),
        suggestions=[],
    )
    already = Break(
        break_id=uuid4(),
        break_type="quantity_break",
        status="open",
        symbol="MSFT",
        trade_date=date(2024, 6, 3),
        created_at=datetime.now(timezone.utc),
    )
    sugg = ResolutionSuggestion(
        suggestion_id=uuid4(),
        break_id=already.break_id,
        root_cause="quantity_mismatch",
        confidence=0.5,
        explanation="already done",
        suggested_action="amend_quantity",
        evidence=[],
        created_at=datetime.now(timezone.utc),
    )
    already.suggestions = [sugg]
    session.add_all([missing, already, sugg])
    session.flush()

    monkeypatch.setattr(
        "backend.agent.auto_investigate.provider_from_env",
        lambda *_a, **_k: StubProvider(),
    )
    monkeypatch.setattr(
        "backend.agent.auto_investigate.cache_dir_from_env",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "backend.agent.auto_investigate.s3_cache_settings",
        lambda: (None, "market-data", "us-east-1"),
    )

    open_ids = {row.break_id for row in open_breaks_without_suggestions(session)}
    assert missing.break_id in open_ids
    assert already.break_id not in open_ids

    summary = investigate_missing_suggestions(session, provider_name="stub")
    assert summary["attempted"] == 1
    assert summary["written"] == 1
    assert summary["failed"] == 0
    rows = list(session.scalars(select(ResolutionSuggestion)).all())
    assert any(row.break_id == missing.break_id for row in rows)
    session.close()


def test_run_daily_blotter_stays_callable_without_agent(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("S3_CACHE_BUCKET", raising=False)
    # Smoke: function is importable from pipeline without agent.
    assert callable(run_daily_blotter)
