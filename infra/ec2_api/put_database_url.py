"""Store DATABASE_URL from a local .env into SSM as a SecureString.

Never prints the URL or password. Run from the repo root:

    AWS_PROFILE=trade-recon-8948 uv run python infra/ec2_api/put_database_url.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

PARAM_NAME = "/trade-recon/database-url"
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_database_url(env_path: Path) -> str:
    if not env_path.is_file():
        raise FileNotFoundError(f"Missing {env_path}; copy .env.example and set DATABASE_URL")
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "DATABASE_URL":
            continue
        url = value.strip().strip("'").strip('"')
        if url:
            return url
    raise RuntimeError("DATABASE_URL is not set in .env")


def main() -> int:
    url = _read_database_url(REPO_ROOT / ".env")
    client = boto3.client("ssm", region_name=REGION)
    try:
        client.put_parameter(
            Name=PARAM_NAME,
            Value=url,
            Type="SecureString",
            Overwrite=True,
            Description="SQLAlchemy DATABASE_URL for trade-recon API (EC2). Do not commit.",
        )
    except ClientError as exc:
        print(f"Failed to write SSM parameter {PARAM_NAME}: {exc}", file=sys.stderr)
        return 1
    print(f"Updated SSM SecureString {PARAM_NAME} in {REGION} (value not shown)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
