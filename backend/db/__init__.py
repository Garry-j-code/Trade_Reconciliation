"""SQLAlchemy models and DB session helpers for trade reconciliation.

Local Postgres (or RDS) is optional for step 3 — normalization works as
dataframe → Parquet without ``DATABASE_URL``. See ``backend/db/schema.sql``.
"""

from backend.db.models import Base
from backend.db.session import (
    create_all_tables,
    database_url_from_env,
    get_engine,
    get_session_factory,
)

__all__ = [
    "Base",
    "create_all_tables",
    "database_url_from_env",
    "get_engine",
    "get_session_factory",
]
