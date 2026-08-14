"""Minimal Step Functions stub handler — replace with real pipeline Lambdas in Phase 2."""

from __future__ import annotations

import json
from typing import Any


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    stage = event.get("stage", "unknown")
    run_id = event.get("run_id", "n/a")
    return {
        "ok": True,
        "stage": stage,
        "run_id": run_id,
        "message": f"stub {stage} for run {run_id}",
    }


# Allow local smoke: python pipeline_stub.py
if __name__ == "__main__":
    print(json.dumps(handler({"stage": "ingest", "run_id": "local"}, None)))
