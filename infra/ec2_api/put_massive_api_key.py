"""Store MASSIVE_API_KEY from local .env into SSM as a SecureString. Never prints the key."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

PARAM_NAME = "/trade-recon/massive-api-key"
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_key(env_path: Path) -> str:
    if not env_path.is_file():
        raise FileNotFoundError(f"Missing {env_path}; set MASSIVE_API_KEY in .env")
    found: str | None = None
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        name = key.strip()
        if name not in {"MASSIVE_API_KEY", "POLYGON_API_KEY"}:
            continue
        secret = value.strip().strip("'").strip('"')
        if secret:
            found = secret
            if name == "MASSIVE_API_KEY":
                break
    if not found:
        raise RuntimeError("MASSIVE_API_KEY (or POLYGON_API_KEY) is not set in .env")
    return found


def main() -> int:
    secret = _read_key(REPO_ROOT / ".env")
    client = boto3.client("ssm", region_name=REGION)
    try:
        client.put_parameter(
            Name=PARAM_NAME,
            Value=secret,
            Type="SecureString",
            Overwrite=True,
            Description="Massive/Polygon API key for daily blotter fetch on EC2. Do not commit.",
        )
    except ClientError as exc:
        print(f"Failed to write SSM parameter {PARAM_NAME}: {exc}", file=sys.stderr)
        return 1
    print(f"Updated SSM SecureString {PARAM_NAME} in {REGION} (value not shown)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
