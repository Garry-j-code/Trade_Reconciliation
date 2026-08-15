"""Engine / session helpers. DB is optional — normalization works without it."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.db.models import Base

logger = logging.getLogger(__name__)


def database_url_from_env(env: Optional[dict[str, str]] = None) -> Optional[str]:
    """Return ``DATABASE_URL`` if set and non-empty, else ``None``."""
    source = env if env is not None else os.environ
    url = (source.get("DATABASE_URL") or "").strip()
    return url or None


def get_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine for Postgres (psycopg3 driver)."""
    return create_engine(
        database_url, echo=echo, future=True, pool_pre_ping=True
    )


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def ensure_pgvector(engine: Engine) -> None:
    """Create the ``vector`` extension when connected to Postgres."""
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


def ensure_agent_memory_table(engine: Engine) -> None:
    """Create ``agent_memory`` if a pre-step-6 RDS is missing it; add HITL columns."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS agent_memory (
                    memory_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    scope TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding VECTOR(1536),
                    source_break_ids UUID[],
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        conn.execute(
            text("ALTER TABLE agent_memory ADD COLUMN IF NOT EXISTS audit_id UUID")
        )
        conn.execute(
            text("ALTER TABLE agent_memory ADD COLUMN IF NOT EXISTS facts JSONB")
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_memory_audit_id "
                "ON agent_memory (audit_id) WHERE audit_id IS NOT NULL"
            )
        )


def ensure_agent_schema_patches(engine: Engine) -> None:
    """Add columns introduced after the initial create_all (idempotent)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE resolution_suggestions "
                "ADD COLUMN IF NOT EXISTS inferred BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        for table in (
            "raw_broker_trades",
            "raw_desk_trades",
            "normalized_trades",
            "breaks",
        ):
            conn.execute(
                text(
                    f"ALTER TABLE {table} "
                    "ADD COLUMN IF NOT EXISTS executed_at TIMESTAMPTZ"
                )
            )
    try:
        ensure_agent_memory_table(engine)
    except Exception as exc:  # noqa: BLE001 — sqlite / missing extension
        logger.warning("Could not patch agent_memory: %s", exc)
        for table in (
            "raw_broker_trades",
            "raw_desk_trades",
            "normalized_trades",
        ):
            conn.execute(
                text(
                    f"ALTER TABLE {table} "
                    "ADD COLUMN IF NOT EXISTS settlement_datetime TIMESTAMPTZ"
                )
            )


def ensure_audit_log_survives_break_delete(engine: Engine) -> None:
    """Rematch may replace ``breaks`` rows; keep ``audit_log`` (ON DELETE SET NULL)."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log ALTER COLUMN break_id DROP NOT NULL"))
        conn.execute(
            text("ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_break_id_fkey")
        )
        conn.execute(
            text(
                "ALTER TABLE audit_log ADD CONSTRAINT audit_log_break_id_fkey "
                "FOREIGN KEY (break_id) REFERENCES breaks(break_id) ON DELETE SET NULL"
            )
        )


def create_all_tables(engine: Engine, *, with_pgvector: bool = True) -> None:
    """Create all ORM tables. Enables pgvector first when requested."""
    if with_pgvector:
        try:
            ensure_pgvector(engine)
        except Exception as exc:  # noqa: BLE001 — surface, then continue for non-pg
            logger.warning("Could not enable pgvector extension: %s", exc)
    Base.metadata.create_all(engine)
    try:
        ensure_agent_schema_patches(engine)
    except Exception as exc:  # noqa: BLE001 — sqlite / missing table
        logger.warning("Could not apply agent schema patches: %s", exc)
    try:
        ensure_audit_log_survives_break_delete(engine)
    except Exception as exc:  # noqa: BLE001 — sqlite / missing table
        logger.warning("Could not patch audit_log FK: %s", exc)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, rollback on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
