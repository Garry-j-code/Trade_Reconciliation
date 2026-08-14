"""Auth gate: /health public; /api/* requires Cognito when configured."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.api.auth import AuthContext, reset_jwks_client
from backend.api.deps import get_db
from backend.api.main import create_app


@pytest.fixture
def auth_app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_TestPool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "testclientid")
    monkeypatch.setenv("COGNITO_REGION", "us-east-1")
    monkeypatch.setenv("RECON_SCHEDULER_SECRET", "scheduler-secret-value")
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    reset_jwks_client()
    app = create_app()
    yield app
    reset_jwks_client()


@pytest.fixture
def auth_client(auth_app) -> TestClient:
    with TestClient(auth_app) as client:
        yield client


def test_health_public_when_auth_on(auth_client: TestClient) -> None:
    response = auth_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_summary_unauthenticated_is_401(auth_client: TestClient) -> None:
    response = auth_client.get("/api/summary")
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


def test_me_unauthenticated_is_401(auth_client: TestClient) -> None:
    assert auth_client.get("/api/me").status_code == 401


def test_summary_with_jwt_reaches_handler(auth_app, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.api.auth.verify_cognito_jwt",
        lambda _token: AuthContext(
            sub="abc",
            username="analyst@traderecon.demo",
            email="analyst@traderecon.demo",
            token_use="id",
        ),
    )

    def _db():
        yield object()

    auth_app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(
        "backend.api.crud.summary_stats",
        lambda _s: {
            "total_trades": 1,
            "match_count": 1,
            "break_count": 0,
            "open_break_count": 0,
            "pct_clean_matched": 100.0,
            "breaks_by_type": [],
            "notional_at_risk": 0.0,
        },
    )
    with TestClient(auth_app) as client:
        denied = client.get("/api/summary")
        ok = client.get("/api/summary", headers={"Authorization": "Bearer fake.jwt"})
        me = client.get("/api/me", headers={"Authorization": "Bearer fake.jwt"})
    assert denied.status_code == 401
    assert ok.status_code == 200
    assert ok.json()["total_trades"] == 1
    assert me.status_code == 200
    assert me.json()["email"] == "analyst@traderecon.demo"
    auth_app.dependency_overrides.clear()


def test_scheduler_secret_allows_recon_not_summary(
    auth_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "backend.api.routes.recon.run_recon_capped",
        lambda **_k: MagicMock(
            broker_rows=1,
            desk_rows=1,
            normalized_rows=2,
            match_count=1,
            break_count=0,
            breaks_by_type={},
            elapsed_seconds=0.1,
            db_loaded=False,
        ),
    )
    headers = {"X-Recon-Scheduler-Secret": "scheduler-secret-value"}
    with TestClient(auth_app) as client:
        summary = client.get("/api/summary", headers=headers)
        recon = client.post("/api/recon/run", headers=headers, json={"replace": True})
    assert summary.status_code == 403
    assert recon.status_code == 200
    assert recon.json()["match_count"] == 1
