"""Pause / resume product compute so the demo can stop billing after ~1 month.

Does **not** delete the market-data S3 bucket or RDS. Stopping RDS still bills
for storage; AWS also auto-restarts a stopped instance after 7 days.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"
DEFAULT_RDS_ID = "trade-recon-postgres"
DEFAULT_PROJECT = "trade-recon"
SUNSET_PARAM = "/trade-recon/product-sunset-date"

RULE_NAMES: tuple[str, ...] = (
    "trade-recon-weekday-recon",
    "trade-recon-daily-memory",
    "trade-recon-daily-blotter",
    "trade-recon-sunset-watch",
)


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )


def _rds_id() -> str:
    return os.environ.get("TRADE_RECON_RDS_ID") or DEFAULT_RDS_ID


def find_api_instance_id(ec2: Any) -> str | None:
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": [DEFAULT_PROJECT]},
            {"Name": "tag:Name", "Values": [f"{DEFAULT_PROJECT}-api"]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )
    for reservation in resp.get("Reservations") or []:
        for inst in reservation.get("Instances") or []:
            iid = inst.get("InstanceId")
            if iid:
                return str(iid)
    return None


def disable_eventbridge_rules(events: Any, names: tuple[str, ...] = RULE_NAMES) -> list[str]:
    disabled: list[str] = []
    for name in names:
        try:
            events.disable_rule(Name=name)
            disabled.append(name)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"ResourceNotFoundException", "ValidationException"}:
                logger.info("Rule %s not found; skip", name)
                continue
            raise
    return disabled


def enable_eventbridge_rules(events: Any, names: tuple[str, ...] = RULE_NAMES) -> list[str]:
    enabled: list[str] = []
    skip = {"trade-recon-sunset-watch"}
    for name in names:
        if name in skip:
            continue
        try:
            events.enable_rule(Name=name)
            enabled.append(name)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"ResourceNotFoundException", "ValidationException"}:
                logger.info("Rule %s not found; skip", name)
                continue
            raise
    return enabled


def stop_product(*, wait: bool = False) -> dict[str, Any]:
    """Stop EC2 + RDS and disable schedules. Storage still bills on RDS."""
    region = _region()
    ec2 = boto3.client("ec2", region_name=region)
    rds = boto3.client("rds", region_name=region)
    events = boto3.client("events", region_name=region)
    instance_id = find_api_instance_id(ec2)
    out: dict[str, Any] = {
        "region": region,
        "instance_id": instance_id,
        "rds_id": _rds_id(),
        "rules_disabled": disable_eventbridge_rules(events),
    }
    if instance_id:
        ec2.stop_instances(InstanceIds=[instance_id])
        out["ec2"] = "stop_requested"
    else:
        out["ec2"] = "not_found"
    try:
        rds.stop_db_instance(DBInstanceIdentifier=_rds_id())
        out["rds"] = "stop_requested"
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        out["rds"] = f"skip:{code or type(exc).__name__}"
        logger.warning("RDS stop skipped: %s", code)
    out["notes"] = [
        "RDS storage still bills while stopped (~GB-month).",
        "AWS restarts a stopped RDS instance after 7 days unless you delete it.",
        "Market-data S3 bucket was not modified.",
        "CloudFront still has a small monthly footprint until cdk destroy.",
    ]
    _ = wait
    return out


def start_product(*, wait: bool = False) -> dict[str, Any]:
    region = _region()
    ec2 = boto3.client("ec2", region_name=region)
    rds = boto3.client("rds", region_name=region)
    events = boto3.client("events", region_name=region)
    instance_id = find_api_instance_id(ec2)
    out: dict[str, Any] = {
        "region": region,
        "instance_id": instance_id,
        "rds_id": _rds_id(),
        "rules_enabled": enable_eventbridge_rules(events),
    }
    try:
        rds.start_db_instance(DBInstanceIdentifier=_rds_id())
        out["rds"] = "start_requested"
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        out["rds"] = f"skip:{code or type(exc).__name__}"
        logger.warning("RDS start skipped: %s", code)
    if instance_id:
        ec2.start_instances(InstanceIds=[instance_id])
        out["ec2"] = "start_requested"
    else:
        out["ec2"] = "not_found"
    out["notes"] = [
        "RDS start often takes several minutes; API health will lag.",
        "Re-enable /trade-recon/product-sunset-date if you extended the demo.",
    ]
    _ = wait
    return out


def read_sunset_date(ssm: Any | None = None) -> str | None:
    client = ssm or boto3.client("ssm", region_name=_region())
    try:
        resp = client.get_parameter(Name=SUNSET_PARAM)
    except ClientError:
        return None
    return str((resp.get("Parameter") or {}).get("Value") or "").strip() or None


def maybe_stop_if_sunset(*, force: bool = False, today: date | None = None) -> dict[str, Any]:
    as_of = today or date.today()
    raw = read_sunset_date()
    if force:
        result = stop_product()
        result["sunset_date"] = raw
        result["triggered"] = True
        return result
    if not raw:
        return {"triggered": False, "reason": "no_sunset_param", "sunset_date": None}
    try:
        sunset = date.fromisoformat(raw[:10])
    except ValueError:
        return {"triggered": False, "reason": "bad_sunset_param", "sunset_date": raw}
    if as_of < sunset:
        return {
            "triggered": False,
            "reason": "before_sunset",
            "sunset_date": sunset.isoformat(),
            "today": as_of.isoformat(),
        }
    result = stop_product()
    result["sunset_date"] = sunset.isoformat()
    result["triggered"] = True
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pause or resume trade-recon AWS compute")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stop", help="Disable schedules, stop EC2, stop RDS")
    sub.add_parser("start", help="Start RDS + EC2, re-enable schedules")
    sub.add_parser("status-sunset", help="Print SSM PRODUCT sunset date (not a secret)")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "stop":
        payload = stop_product()
    elif args.command == "start":
        payload = start_product()
    else:
        payload = {
            "sunset_date": read_sunset_date(),
            "as_of": datetime.now(timezone.utc).date().isoformat(),
        }
    print(json.dumps(payload, indent=2, default=str))
    return 0


def main_stop() -> int:
    return main(["stop"])


def main_start() -> int:
    return main(["start"])


if __name__ == "__main__":
    sys.exit(main())
