"""EventBridge sunset watcher: stop EC2 + RDS and disable rules. No destroys."""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

SUNSET_PARAM = os.environ.get("SUNSET_PARAM", "/trade-recon/product-sunset-date")
RDS_ID = os.environ.get("RDS_ID", "trade-recon-postgres")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "")
RULE_NAMES = [
    n.strip()
    for n in (os.environ.get("RULE_NAMES") or "").split(",")
    if n.strip()
]


def _today() -> date:
    return datetime.now(timezone.utc).date()


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    _ = event, context
    ssm = boto3.client("ssm")
    force = bool((event or {}).get("force"))
    sunset_raw = ""
    try:
        sunset_raw = str(
            ssm.get_parameter(Name=SUNSET_PARAM)["Parameter"]["Value"]
        ).strip()
    except ClientError:
        sunset_raw = ""
    if not force:
        if not sunset_raw:
            return {"triggered": False, "reason": "no_sunset_param"}
        try:
            sunset = date.fromisoformat(sunset_raw[:10])
        except ValueError:
            return {"triggered": False, "reason": "bad_sunset_param", "value": sunset_raw[:10]}
        if _today() < sunset:
            return {
                "triggered": False,
                "reason": "before_sunset",
                "sunset_date": sunset.isoformat(),
            }

    out: dict[str, Any] = {
        "triggered": True,
        "sunset_date": sunset_raw[:10] if sunset_raw else None,
        "force": force,
    }
    events = boto3.client("events")
    disabled = []
    for name in RULE_NAMES:
        try:
            events.disable_rule(Name=name)
            disabled.append(name)
        except ClientError:
            continue
    out["rules_disabled"] = disabled
    if INSTANCE_ID:
        boto3.client("ec2").stop_instances(InstanceIds=[INSTANCE_ID])
        out["ec2"] = "stop_requested"
    try:
        boto3.client("rds").stop_db_instance(DBInstanceIdentifier=RDS_ID)
        out["rds"] = "stop_requested"
    except ClientError as exc:
        out["rds"] = exc.response.get("Error", {}).get("Code", "error")
    return out
