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
INSTANCE_NAME_TAG = os.environ.get("INSTANCE_NAME_TAG", "")
RULE_NAMES = [
    n.strip()
    for n in (os.environ.get("RULE_NAMES") or "").split(",")
    if n.strip()
]


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _api_instance_ids() -> list[str]:
    """Instances to stop: the explicit id, else whatever carries the Name tag.

    Resolving by tag matters because an Ec2ApiStack deploy replaces the
    instance; a stale hardcoded id would leave the box running and billing.
    """
    if INSTANCE_ID:
        return [INSTANCE_ID]
    if not INSTANCE_NAME_TAG:
        return []
    resp = boto3.client("ec2").describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [INSTANCE_NAME_TAG]},
            {"Name": "instance-state-name", "Values": ["pending", "running"]},
        ]
    )
    return [
        inst["InstanceId"]
        for res in resp.get("Reservations", [])
        for inst in res.get("Instances", [])
    ]


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    force = bool((event or {}).get("force"))
    ssm = boto3.client("ssm")
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
            return {"triggered": False, "reason": "bad_sunset_param"}
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
    instance_ids = _api_instance_ids()
    if instance_ids:
        boto3.client("ec2").stop_instances(InstanceIds=instance_ids)
        out["ec2"] = "stop_requested"
    try:
        boto3.client("rds").stop_db_instance(DBInstanceIdentifier=RDS_ID)
        out["rds"] = "stop_requested"
    except ClientError as exc:
        out["rds"] = exc.response.get("Error", {}).get("Code", "error")
    return out
