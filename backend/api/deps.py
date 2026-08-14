"""FastAPI dependencies: SQLAlchemy session from ``DATABASE_URL``."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Optional

from fastapi import Header, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from backend.api.auth import AuthContext, auth_is_required
from backend.api.models import ACTOR_HEADER, DEFAULT_ACTOR
from backend.db.session import (
    database_url_from_env,
    get_engine,
    get_session_factory,
)


def _is_testing() -> bool:
    return bool(os.environ.get("TESTING") or os.environ.get("PYTEST_CURRENT_TEST"))

_session_factory: sessionmaker[Session] | None = None


def reset_session_factory() -> None:
    """Drop the cached factory (tests)."""
    global _session_factory
    _session_factory = None


def get_session_factory_cached() -> sessionmaker[Session] | None:
    """Build a process-wide session factory from ``DATABASE_URL`` if set."""
    global _session_factory
    if _session_factory is not None:
        return _session_factory
    url = database_url_from_env()
    if not url:
        return None
    _session_factory = get_session_factory(get_engine(url))
    return _session_factory


def session_factory_from_app(request: Request) -> sessionmaker[Session] | None:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is not None:
        return factory
    # Unit tests must not pick up a developer DATABASE_URL from the env.
    if _is_testing():
        return None
    return get_session_factory_cached()


def get_db(request: Request) -> Iterator[Session]:
    """Yield a request-scoped session. 503 when Postgres is not configured."""
    factory = session_factory_from_app(request)
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL is not configured",
        )
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db_optional(request: Request) -> Iterator[Optional[Session]]:
    """Like ``get_db`` but yields ``None`` instead of 503 (health)."""
    factory = session_factory_from_app(request)
    if factory is None:
        yield None
        return
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def resolve_actor(
    request: Request,
    x_actor: str | None = Header(default=None, alias=ACTOR_HEADER),
) -> str:
    """Human actor for the approval gate. Cognito identity wins when auth is on."""
    auth = getattr(request.state, "auth", None)
    if isinstance(auth, AuthContext) and auth.actor:
        return auth.actor
    if auth_is_required():
        return DEFAULT_ACTOR
    if x_actor and x_actor.strip():
        return x_actor.strip()
    return DEFAULT_ACTOR
