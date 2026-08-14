"""Invoke hosted recon or the on-instance memory writer. No DATABASE_URL here."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

import boto3


def _scheduler_secret() -> str:
    arn = os.environ["SCHEDULER_SECRET_ARN"]
    sm = boto3.client("secretsmanager")
    return sm.get_secret_value(SecretId=arn)["SecretString"]


def _post_recon() -> dict[str, Any]:
    base = os.environ["API_BASE_URL"].rstrip("/")
    url = f"{base}/api/recon/run"
    body = json.dumps({"replace": True}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Recon-Scheduler-Secret": _scheduler_secret(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=170) as resp:
            raw = resp.read().decode("utf-8")
            parsed: Any = json.loads(raw) if raw else {}
            return {"ok": True, "status": resp.status, "body": parsed}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:2000]
        return {"ok": False, "status": exc.code, "body": err_body}


def _run_memory_writer() -> dict[str, Any]:
    instance_id = os.environ["INSTANCE_ID"]
    ssm = boto3.client("ssm")
    script = (
        "set -euo pipefail\n"
        "set +x\n"
        "set -a\n"
        "source /etc/trade-recon/api.env\n"
        "set +a\n"
        "cd /opt/trade-recon/app\n"
        "export PYTHONPATH=/opt/trade-recon/app\n"
        "/opt/trade-recon/venv/bin/python -m backend.agent.memory_writer "
        "--provider stub --no-semantic\n"
    )
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Comment="trade-recon memory writer (stub provider; Bedrock cost cap)",
        Parameters={"commands": [script]},
        TimeoutSeconds=120,
    )
    command_id = resp["Command"]["CommandId"]
    return {"ok": True, "command_id": command_id}


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    payload = event or {}
    if isinstance(payload.get("Payload"), dict):
        payload = payload["Payload"]
    action = str(payload.get("action") or payload.get("Action") or "recon")
    if action == "memory":
        return _run_memory_writer()
    return _post_recon()
