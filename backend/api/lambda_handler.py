"""AWS Lambda entrypoint for FastAPI via Mangum (Phase 2 — not used by default CDK deploy).

Local development remains ``uv run serve-api``. Enabling this path requires:

1. Packaging backend deps for Lambda (container image recommended; zip is painful with pandas).
2. VPC networking to RDS without opening ``0.0.0.0/0`` (and without NAT if budget-bound).
3. ``pip install mangum`` (or add to the Lambda image).

CDK v1 deploys an API *stub* when ``-c enableApi=true``; it does not ship this handler yet.
"""

from __future__ import annotations

from backend.api.main import app

try:
    from mangum import Mangum
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "mangum is required for Lambda. Install mangum in the Lambda image/layer, "
        "or keep using local `uv run serve-api`."
    ) from exc

handler = Mangum(app, lifespan="auto")
