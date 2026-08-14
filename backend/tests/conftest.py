"""Shared pytest fixtures. Unit tests never require live RDS."""

from __future__ import annotations

import os

# Prevent FastAPI lifespan from loading .env / opening RDS during unit tests.
os.environ.setdefault("TESTING", "1")
