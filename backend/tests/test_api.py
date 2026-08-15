"""FastAPI route tests — DB mocked; no live RDS."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.api.deps import get_db, get_db_optional
from backend.api.main import create_app
from backend.api.models import (
    AUDIT_APPROVED,
    AUDIT_OVERRIDDEN,
    AUDIT_REJECTED,
    BREAK_STATUS_OVERRIDDEN,
    BREAK_STATUS_REJECTED,
    BREAK_STATUS_RESOLVED,
    REVIEW_MANUAL,
    REVIEW_ONE_CLICK,
)
from backend.api.services import approve_break, override_break, reject_break, review_routing
from backend.db.models import AuditLog, Break, ResolutionSuggestion
from backend.pipeline.recon import ReconRunResult, ReconTimeoutError


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def test_health_without_db(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "unavailable"


def test_health_connected(app) -> None:
    class _Session:
        pass

    def _db():
        yield _Session()

    app.dependency_overrides[get_db_optional] = _db

    def _ping(_session: Any) -> bool:
        return True

    from backend.api.routes import health as health_routes

    original = health_routes.crud.ping_db
    health_routes.crud.ping_db = _ping  # type: ignore[method-assign]
    try:
        with TestClient(app) as client:
            body = client.get("/health").json()
            assert body["db"] == "connected"
    finally:
        health_routes.crud.ping_db = original  # type: ignore[method-assign]
        app.dependency_overrides.clear()


def test_summary_uses_crud(app, monkeypatch: pytest.MonkeyPatch) -> None:
    def _db():
        yield object()

    app.dependency_overrides[get_db] = _db
    captured: dict[str, Any] = {}

    def _stats(_s: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "total_trades": 360,
            "pair_count": 360,
            "broker_leg_count": 393,
            "desk_leg_count": 342,
            "match_count": 280,
            "matched_pair_count": 280,
            "match_row_count": 321,
            "break_count": 80,
            "open_break_count": 80,
            "pct_clean_matched": 77.7778,
            "breaks_by_type": [{"break_type": "price_break", "count": 3}],
            "notional_at_risk": 125000.5,
        }

    monkeypatch.setattr("backend.api.crud.summary_stats", _stats)
    with TestClient(app) as client:
        response = client.get("/api/summary")
        ranged = client.get(
            "/api/summary",
            params={"from_date": "2024-06-03", "to_date": "2024-06-10"},
        )
        inverted = client.get(
            "/api/summary",
            params={"from_date": "2024-06-10", "to_date": "2024-06-03"},
        )
    assert response.status_code == 200
    assert ranged.status_code == 200
    assert inverted.status_code == 422
    assert captured["from_date"] == date(2024, 6, 3)
    assert captured["to_date"] == date(2024, 6, 10)
    body = response.json()
    assert body["total_trades"] == 360
    assert body["pair_count"] == 360
    assert body["match_count"] == 280
    assert body["matched_pair_count"] == 280
    assert body["match_row_count"] == 321
    assert body["pct_clean_matched"] == 77.7778
    assert body["broker_leg_count"] == 393
    assert body["breaks_by_type"][0]["break_type"] == "price_break"
    app.dependency_overrides.clear()


def test_summary_requires_db(client: TestClient) -> None:
    response = client.get("/api/summary")
    assert response.status_code == 503


def test_breaks_list_and_filters(app, monkeypatch: pytest.MonkeyPatch) -> None:
    break_id = uuid4()
    row = Break(
        break_id=break_id,
        break_type="price_break",
        status="open",
        symbol="AAPL",
        trade_date=date(2024, 6, 3),
        pair_id="PAIR-1",
        detail={"desk": "EQ-US", "notional_at_risk": 19000.0},
        created_at=datetime.now(timezone.utc),
    )

    captured: dict[str, Any] = {}

    def _list(_session: Any, **kwargs: Any) -> tuple[list[Break], int]:
        captured.update(kwargs)
        return [row], 1

    def _db():
        yield object()

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr("backend.api.crud.list_breaks", _list)
    monkeypatch.setattr("backend.api.crud.latest_audits_by_break", lambda *_a, **_k: {})
    with TestClient(app) as client:
        response = client.get(
            "/api/breaks",
            params={
                "desk": "EQ-US",
                "symbol": "AAPL",
                "break_type": "price_break",
                "date": "2024-06-03",
                "page": 1,
                "page_size": 10,
                "sort": "notional",
                "order": "asc",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["break_id"] == str(break_id)
    assert body["items"][0]["desk"] == "EQ-US"
    assert captured["desk"] == "EQ-US"
    assert captured["symbol"] == "AAPL"
    assert captured["sort"] == "notional"
    assert captured["order"] == "asc"
    assert captured["date_from"] == date(2024, 6, 3)
    assert captured["date_to"] == date(2024, 6, 3)
    app.dependency_overrides.clear()


def test_breaks_list_date_range_params(app, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def _list(_session: Any, **kwargs: Any) -> tuple[list[Break], int]:
        calls.append(kwargs)
        return [], 0

    def _db():
        yield object()

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr("backend.api.crud.list_breaks", _list)
    monkeypatch.setattr("backend.api.crud.latest_audits_by_break", lambda *_a, **_k: {})
    with TestClient(app) as client:
        empty = client.get("/api/breaks")
        ranged = client.get(
            "/api/breaks",
            params={"from_date": "2024-06-03", "to_date": "2024-06-10", "status": "open"},
        )
        inverted = client.get(
            "/api/breaks",
            params={"from_date": "2024-06-10", "to_date": "2024-06-03"},
        )
        aliases = client.get(
            "/api/breaks",
            params={"date_from": "2024-06-04", "date_to": "2024-06-05"},
        )
    assert empty.status_code == 200
    assert ranged.status_code == 200
    assert inverted.status_code == 422
    assert aliases.status_code == 200
    assert calls[0]["date_from"] is None
    assert calls[0]["date_to"] is None
    assert calls[1]["date_from"] == date(2024, 6, 3)
    assert calls[1]["date_to"] == date(2024, 6, 10)
    assert calls[1]["status"] == "open"
    assert calls[2]["date_from"] == date(2024, 6, 4)
    assert calls[2]["date_to"] == date(2024, 6, 5)
    app.dependency_overrides.clear()


def test_breaks_list_status_filters(app, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def _list(_session: Any, **kwargs: Any) -> tuple[list[Break], int]:
        calls.append(kwargs)
        return [], 0

    def _db():
        yield object()

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr("backend.api.crud.list_breaks", _list)
    monkeypatch.setattr("backend.api.crud.latest_audits_by_break", lambda *_a, **_k: {})
    with TestClient(app) as client:
        resolved = client.get("/api/breaks", params={"status": "resolved"})
        rejected = client.get("/api/breaks", params={"status": "rejected"})
        overridden = client.get("/api/breaks", params={"status": "overridden"})
        all_statuses = client.get("/api/breaks", params={"status": "all"})
        invalid = client.get("/api/breaks", params={"status": "closed"})
        open_and_type = client.get(
            "/api/breaks", params={"status": "open", "break_type": "price_break"}
        )
    assert resolved.status_code == 200
    assert rejected.status_code == 200
    assert overridden.status_code == 200
    assert all_statuses.status_code == 200
    assert invalid.status_code == 422
    assert open_and_type.status_code == 200
    assert calls[0]["status"] == "resolved"
    assert calls[1]["status"] == "rejected"
    assert calls[2]["status"] == "overridden"
    assert calls[3]["status"] is None
    assert calls[4]["status"] == "open"
    assert calls[4]["break_type"] == "price_break"
    app.dependency_overrides.clear()


def test_breaks_rejects_invalid_sort(app) -> None:
    def _db():
        yield object()

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as client:
        response = client.get("/api/breaks", params={"sort": "created_at", "order": "sideways"})
    assert response.status_code == 422
    app.dependency_overrides.clear()


def test_break_detail_placeholder_suggestion(app, monkeypatch: pytest.MonkeyPatch) -> None:
    break_id = uuid4()
    row = Break(
        break_id=break_id,
        break_type="missing_desk",
        status="open",
        symbol="MSFT",
        trade_date=date(2024, 6, 3),
        broker_trade_ids="BRK-1",
        desk_trade_ids="",
        detail={"desk": None, "notional_at_risk": 500.0},
        suggestions=[],
        created_at=datetime.now(timezone.utc),
    )

    def _db():
        yield object()

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr("backend.api.crud.get_break", lambda _s, _id: row)
    monkeypatch.setattr("backend.api.crud.get_normalized_by_ids", lambda *_a, **_k: [])
    monkeypatch.setattr("backend.api.crud.get_raw_broker", lambda *_a, **_k: [])
    monkeypatch.setattr("backend.api.crud.get_raw_desk", lambda *_a, **_k: [])
    monkeypatch.setattr("backend.api.crud.latest_suggestion", lambda *_a, **_k: None)
    monkeypatch.setattr("backend.api.crud.list_audits_for_break", lambda *_a, **_k: [])
    with TestClient(app) as client:
        response = client.get(f"/api/breaks/{break_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["suggestion"]["break_id"] == str(break_id)
    assert body["suggestion"]["root_cause"] is None
    assert body["suggestion"]["evidence"] == []
    assert body["review_routing"] == REVIEW_MANUAL
    assert body["broker_side"]["trade_ids"] == ["BRK-1"]
    app.dependency_overrides.clear()


def test_break_detail_404(app, monkeypatch: pytest.MonkeyPatch) -> None:
    def _db():
        yield object()

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr("backend.api.crud.get_break", lambda *_a, **_k: None)
    with TestClient(app) as client:
        response = client.get(f"/api/breaks/{uuid4()}")
    assert response.status_code == 404
    app.dependency_overrides.clear()


def test_matches_list(app, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.db.models import Match

    match = Match(
        match_id=uuid4(),
        broker_trade_id="BRK-1",
        desk_trade_id="DSK-1",
        pair_id="PAIR-1",
        match_pass="exact",
        created_at=datetime.now(timezone.utc),
    )

    def _db():
        yield object()

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr("backend.api.crud.list_matches", lambda *_a, **_k: ([match], 1))
    with TestClient(app) as client:
        response = client.get("/api/matches")
    assert response.status_code == 200
    assert response.json()["items"][0]["match_pass"] == "exact"
    app.dependency_overrides.clear()


def test_recon_run_mocked(app, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.api.routes.recon.run_rematch_from_db_capped",
        lambda **_k: ReconRunResult(
            broker_rows=10,
            desk_rows=9,
            normalized_rows=19,
            match_count=7,
            break_count=3,
            breaks_by_type={"price_break": 2, "missing_desk": 1},
            elapsed_seconds=0.12,
            db_loaded=True,
        ),
    )
    called: dict[str, Any] = {}

    def _parquet(**_k: Any) -> ReconRunResult:
        called["parquet"] = True
        raise AssertionError("hosted run must not read generated parquet")

    monkeypatch.setattr("backend.api.routes.recon.run_recon_capped", _parquet)
    with TestClient(app) as client:
        response = client.post("/api/recon/run", json={"replace": True})
    assert response.status_code == 200
    body = response.json()
    assert body["match_count"] == 7
    assert body["break_count"] == 3
    assert body["db_loaded"] is True
    assert "parquet" not in called


def test_recon_run_default_rematch_without_parquet(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "backend.api.routes.recon.run_rematch_from_db_capped",
        lambda **_k: ReconRunResult(
            broker_rows=4,
            desk_rows=4,
            normalized_rows=8,
            match_count=3,
            break_count=1,
            breaks_by_type={"price_break": 1},
            elapsed_seconds=0.05,
            db_loaded=True,
        ),
    )

    def _parquet(**_k: Any) -> ReconRunResult:
        raise FileNotFoundError(
            "Missing broker trades: /opt/trade-recon/app/backend/data/generated/broker_trades.parquet"
        )

    monkeypatch.setattr("backend.api.routes.recon.run_recon_capped", _parquet)
    with TestClient(app) as client:
        response = client.post("/api/recon/run", json={})
    assert response.status_code == 200
    assert response.json()["match_count"] == 3
    assert response.json()["normalized_rows"] == 8


def test_recon_run_empty_book(app, monkeypatch: pytest.MonkeyPatch) -> None:
    def _empty(**_k: Any) -> ReconRunResult:
        raise ValueError(
            "No normalized trades in the database. "
            "Run the daily blotter (CLI / EventBridge) before rematching."
        )

    monkeypatch.setattr("backend.api.routes.recon.run_rematch_from_db_capped", _empty)
    with TestClient(app) as client:
        response = client.post("/api/recon/run", json={"mode": "rematch"})
    assert response.status_code == 400
    assert "normalized trades" in response.json()["detail"]


def test_recon_run_timeout(app, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**_k: Any) -> ReconRunResult:
        raise ReconTimeoutError("Recon run exceeded 1s cap")

    monkeypatch.setattr("backend.api.routes.recon.run_rematch_from_db_capped", _boom)
    with TestClient(app) as client:
        response = client.post("/api/recon/run", json={"replace": True})
    assert response.status_code == 504


def test_recon_run_schedules_investigate_without_blocking(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "backend.api.routes.recon.run_rematch_from_db_capped",
        lambda **_k: ReconRunResult(
            broker_rows=4,
            desk_rows=4,
            normalized_rows=8,
            match_count=3,
            break_count=2,
            breaks_by_type={"price_break": 2},
            elapsed_seconds=0.05,
            db_loaded=True,
        ),
    )
    called: dict[str, Any] = {}

    def _schedule() -> str:
        called["scheduled"] = True
        return "queued"

    monkeypatch.setattr(
        "backend.api.routes.recon.schedule_investigate_after_rematch", _schedule
    )
    with TestClient(app) as client:
        response = client.post("/api/recon/run", json={"mode": "rematch"})
    assert response.status_code == 200
    assert called.get("scheduled") is True
    body = response.json()
    assert body["match_count"] == 3
    assert body["investigate_status"] == "queued"
    assert body["investigate_attempted"] is None
    assert body["investigate_written"] is None


def test_recon_http_returns_before_slow_investigate(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from time import monotonic

    monkeypatch.setattr(
        "backend.api.routes.recon.run_rematch_from_db_capped",
        lambda **_k: ReconRunResult(
            broker_rows=2,
            desk_rows=2,
            normalized_rows=4,
            match_count=1,
            break_count=1,
            breaks_by_type={"price_break": 1},
            elapsed_seconds=0.02,
            db_loaded=True,
        ),
    )
    started = threading.Event()
    release = threading.Event()

    def _slow() -> dict[str, Any]:
        started.set()
        release.wait(timeout=5)
        return {"attempted": 1, "written": 1, "failed": 0, "errors": []}

    monkeypatch.setattr("backend.api.routes.recon.investigate_after_rematch", _slow)
    with TestClient(app) as client:
        t0 = monotonic()
        response = client.post("/api/recon/run", json={"mode": "rematch"})
        elapsed = monotonic() - t0
    assert response.status_code == 200
    assert elapsed < 1.0
    assert response.json()["investigate_status"] == "queued"
    assert started.wait(timeout=2)
    release.set()


def test_investigate_after_rematch_swallows_bedrock_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")

    def _boom(*_a: Any, **_k: Any) -> Any:
        from backend.agent.providers import BedrockAccessError

        raise BedrockAccessError("bedrock unavailable")

    monkeypatch.setattr("backend.db.session.get_engine", _boom)
    from backend.api.routes.recon import investigate_after_rematch

    stats = investigate_after_rematch()
    assert stats["written"] == 0
    assert stats["failed"] == 0
    assert stats["errors"]


def test_recon_run_ingest_missing_parquet(app, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**_k: Any) -> ReconRunResult:
        raise FileNotFoundError("Missing broker trades: /tmp/x")

    monkeypatch.setattr("backend.api.routes.recon.run_recon_capped", _boom)
    with TestClient(app) as client:
        response = client.post("/api/recon/run", json={"mode": "ingest"})
    assert response.status_code == 400
    assert "Missing broker trades" in response.json()["detail"]


class _FakeSession:
    def __init__(self, brk: Break) -> None:
        self.brk = brk
        self.added: list[Any] = []

    def get(self, model: type, ident: UUID) -> Break | None:
        if ident == self.brk.break_id:
            return self.brk
        return None

    def add(self, obj: Any) -> None:
        if getattr(obj, "audit_id", None) is None:
            obj.audit_id = uuid4()
        self.added.append(obj)

    def flush(self) -> None:
        return None


def _open_break() -> Break:
    return Break(
        break_id=uuid4(),
        break_type="price_break",
        status="open",
        symbol="AAPL",
        suggestions=[],
        detail={"notional_at_risk": 1000.0},
    )


def test_approve_without_suggestion_rejected_for_price_break() -> None:
    from fastapi import HTTPException

    brk = _open_break()
    session = _FakeSession(brk)
    with pytest.raises(HTTPException) as exc:
        approve_break(session, brk.break_id, actor="analyst")
    assert exc.value.status_code == 400
    assert "Investigate" in str(exc.value.detail)
    assert session.added == []
    assert brk.status == "open"


def _suggestion_for(brk: Break, *, action: str = "no_action") -> ResolutionSuggestion:
    return ResolutionSuggestion(
        suggestion_id=uuid4(),
        break_id=brk.break_id,
        root_cause="calendar_timing",
        confidence=0.4,
        explanation="stub",
        suggested_action=action,
        evidence=[],
        created_at=datetime.now(timezone.utc),
    )


def test_reject_and_override_require_note_and_audit() -> None:
    brk = _open_break()
    session = _FakeSession(brk)
    rejected = reject_break(session, brk.break_id, actor="analyst", note="not a CA")
    assert rejected.status == BREAK_STATUS_REJECTED
    assert session.added[-1].action == AUDIT_REJECTED

    brk.status = "open"
    overridden = override_break(session, brk.break_id, actor="analyst", note="book as-is")
    assert overridden.status == BREAK_STATUS_OVERRIDDEN
    assert session.added[-1].action == AUDIT_OVERRIDDEN
    assert session.added[-1].override_note == "book as-is"


def test_approve_conflict_when_already_resolved() -> None:
    from fastapi import HTTPException

    brk = _open_break()
    brk.status = BREAK_STATUS_RESOLVED
    session = _FakeSession(brk)
    with pytest.raises(HTTPException) as exc:
        approve_break(session, brk.break_id, actor="analyst")
    assert exc.value.status_code == 409
    assert session.added == []


def test_approve_route_never_auto(app) -> None:
    brk = _open_break()
    brk.suggestions = [_suggestion_for(brk, action="no_action")]
    session = _FakeSession(brk)

    def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as client:
        response = client.post(
            f"/api/breaks/{brk.break_id}/approve",
            headers={"X-Actor": "pat"},
        )
        missing = client.post(f"/api/breaks/{uuid4()}/override", json={"note": "x"})
        reject = client.post(
            f"/api/breaks/{brk.break_id}/reject",
            json={"note": "nope"},
        )
    # first approve resolved it; reject should 409
    assert response.status_code == 200
    assert response.json()["action"] == AUDIT_APPROVED
    assert missing.status_code == 404
    assert reject.status_code == 409
    app.dependency_overrides.clear()


def test_approve_audit_records_actor() -> None:
    brk = _open_break()
    brk.suggestions = [_suggestion_for(brk, action="no_action")]
    session = _FakeSession(brk)
    result = approve_break(
        session, brk.break_id, actor="analyst@traderecon.demo", note="looks right"
    )
    assert result.status == BREAK_STATUS_RESOLVED
    audit = session.added[0]
    assert isinstance(audit, AuditLog)
    assert audit.actor == "analyst@traderecon.demo"
    assert audit.override_note == "looks right"
    assert audit.action == AUDIT_APPROVED
    assert audit.agent_suggestion_snapshot["suggested_action"] == "no_action"


def test_approve_writes_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def _record(session: object, *, brk: Break, audit: AuditLog, suggestion: object, embed_fn: object = None) -> None:
        captured.append(audit.action)
        assert brk.break_id == audit.break_id
        assert suggestion is not None

    monkeypatch.setattr(
        "backend.agent.memory_writer.record_human_decision_memory", _record
    )
    brk = _open_break()
    brk.suggestions = [_suggestion_for(brk, action="no_action")]
    session = _FakeSession(brk)
    result = approve_break(session, brk.break_id, actor="analyst", note="ok")
    assert result.action == AUDIT_APPROVED
    assert session.added[0].action == AUDIT_APPROVED
    assert captured == ["approved"]

    brk2 = _open_break()
    brk2.suggestions = [_suggestion_for(brk2, action="no_action")]
    session2 = _FakeSession(brk2)
    reject_break(session2, brk2.break_id, actor="analyst", note="nope")
    assert captured == ["approved", "rejected"]


def test_override_route_requires_note(app) -> None:
    brk = _open_break()

    def _db():
        yield _FakeSession(brk)

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as client:
        response = client.post(f"/api/breaks/{brk.break_id}/override", json={})
    assert response.status_code == 422
    app.dependency_overrides.clear()


def test_override_route_force_closes_and_audits(app) -> None:
    brk = _open_break()
    session = _FakeSession(brk)

    def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as client:
        response = client.post(
            f"/api/breaks/{brk.break_id}/override",
            json={"note": "print is good"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == BREAK_STATUS_OVERRIDDEN
    assert body["action"] == AUDIT_OVERRIDDEN
    assert brk.status == BREAK_STATUS_OVERRIDDEN
    assert session.added[-1].action == AUDIT_OVERRIDDEN
    assert session.added[-1].override_note == "print is good"
    app.dependency_overrides.clear()


def test_investigate_uses_stub_and_persists_suggestion(app, tmp_path) -> None:
    brk = _open_break()
    session = _FakeSession(brk)

    def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as client:
        response = client.post(
            f"/api/breaks/{brk.break_id}/investigate",
            json={"provider": "stub", "tools_enabled": False},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["break_id"] == str(brk.break_id)
    assert body["root_cause"]
    assert body["suggested_action"]
    assert 0.0 <= body["confidence"] <= 1.0
    assert any(isinstance(obj, ResolutionSuggestion) for obj in session.added)
    app.dependency_overrides.clear()


def test_review_routing_thresholds() -> None:
    assert review_routing(None, 10.0) == REVIEW_MANUAL
    suggestion = ResolutionSuggestion(
        suggestion_id=uuid4(),
        break_id=uuid4(),
        root_cause="placeholder",
        confidence=0.9,
        explanation="x",
        suggested_action="placeholder",
        evidence=[],
    )
    assert review_routing(suggestion, 1_000.0) == REVIEW_ONE_CLICK
    assert review_routing(suggestion, 1_000_000.0) == REVIEW_MANUAL
    suggestion.confidence = 0.2
    assert review_routing(suggestion, 1_000.0) == REVIEW_MANUAL
