"""API Gateway / Lambda health stub — not the full FastAPI app.

Full FastAPI+Mangum packaging needs container build + VPC design (see infra/README.md).
Local path remains: ``uv run serve-api``.
"""

from __future__ import annotations

import json
import os
from typing import Any


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    path = "/"
    method = "GET"
    # HTTP API payload v2
    request_context = event.get("requestContext") or {}
    http = request_context.get("http") or {}
    if http:
        path = http.get("path") or path
        method = http.get("method") or method
    else:
        path = event.get("rawPath") or event.get("path") or path
        method = event.get("httpMethod") or method

    body = {
        "service": "trade-recon-api-stub",
        "status": "ok",
        "path": path,
        "method": method,
        "project": os.environ.get("PROJECT", "trade-recon"),
        "rds_identifier": os.environ.get("EXISTING_RDS_IDENTIFIER", ""),
        "note": (
            "Stub only. Real FastAPI runs locally via `uv run serve-api`. "
            "Lambda+RDS needs VPC (no NAT in v1) — see infra/README.md Phase 2."
        ),
    }

    origins = os.environ.get("CORS_ALLOW_ORIGINS", "*")
    allow_origin = origins.split(",")[0].strip() if origins else "*"

    return {
        "statusCode": 200,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": allow_origin,
        },
        "body": json.dumps(body),
    }
