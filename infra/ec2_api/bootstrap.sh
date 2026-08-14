#!/bin/bash
# Idempotent EC2 bootstrap for the FastAPI app. Secrets are never echoed.
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/trade-recon/app}"
VENV="${VENV:-/opt/trade-recon/venv}"
ENV_DIR=/etc/trade-recon
ENV_FILE="${ENV_DIR}/api.env"
SSM_PARAM="${TRADE_RECON_SSM_PARAM:-/trade-recon/database-url}"
REGION="${AWS_REGION:-us-east-1}"
CF_ORIGIN="${CLOUDFRONT_ORIGIN:-https://d1a8rtzx54qkw.cloudfront.net}"
MARKET_BUCKET="${S3_CACHE_BUCKET:-trade-recon-market-data-gagan-8948-us-east-1}"
COGNITO_USER_POOL_ID="${COGNITO_USER_POOL_ID:-}"
COGNITO_CLIENT_ID="${COGNITO_CLIENT_ID:-}"
COGNITO_REGION="${COGNITO_REGION:-$REGION}"
SCHEDULER_SECRET_ARN="${SCHEDULER_SECRET_ARN:-}"

export AWS_DEFAULT_REGION="$REGION"
export AWS_REGION="$REGION"

if [[ ! -f /swapfile ]]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  chmod 600 /swapfile
  mkswap /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon /swapfile || true

dnf install -y python3.12 python3.12-pip unzip awscli || dnf install -y python3.11 python3.11-pip unzip awscli
PY=python3.12
command -v python3.12 >/dev/null 2>&1 || PY=python3.11

mkdir -p "$ENV_DIR" /opt/trade-recon
"$PY" -m venv "$VENV"
"${VENV}/bin/pip" install -U pip wheel setuptools
"${VENV}/bin/pip" install -r "${APP_ROOT}/infra/ec2_api/requirements.txt"
# Do not `pip install .` — hatch force-include duplicates SKILL.md. Run from APP_ROOT via PYTHONPATH.

# Fetch DATABASE_URL without tracing it (set +x).
set +x
umask 077
DB_URL=""
for _ in $(seq 1 24); do
  if DB_URL="$(aws ssm get-parameter --name "$SSM_PARAM" --with-decryption --query Parameter.Value --output text 2>/dev/null)"; then
    if [[ -n "$DB_URL" && "$DB_URL" != "None" && "$DB_URL" != "REPLACE_ME" ]]; then
      break
    fi
  fi
  sleep 5
done
if [[ -z "$DB_URL" || "$DB_URL" == "None" || "$DB_URL" == "REPLACE_ME" ]]; then
  echo "ERROR: SSM parameter ${SSM_PARAM} is missing or still a placeholder" >&2
  exit 1
fi

SCHEDULER_SECRET=""
if [[ -n "$SCHEDULER_SECRET_ARN" ]]; then
  SCHEDULER_SECRET="$(aws secretsmanager get-secret-value --secret-id "$SCHEDULER_SECRET_ARN" --query SecretString --output text 2>/dev/null || true)"
fi

export ENV_FILE DB_URL CF_ORIGIN REGION MARKET_BUCKET
export COGNITO_USER_POOL_ID COGNITO_CLIENT_ID COGNITO_REGION SCHEDULER_SECRET
"$PY" - <<'PY'
import os
from pathlib import Path

env_path = Path(os.environ["ENV_FILE"])
url = os.environ["DB_URL"]

def q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

cf = os.environ["CF_ORIGIN"]
region = os.environ["REGION"]
bucket = os.environ["MARKET_BUCKET"]
lines = [
    f"DATABASE_URL={q(url)}",
    f"AWS_REGION={region}",
    f"AWS_DEFAULT_REGION={region}",
    f"CLOUDFRONT_ORIGIN={cf}",
    f"CORS_ALLOW_ORIGINS={cf},http://localhost:5173,http://127.0.0.1:5173",
    "BEDROCK_MODEL_ID=amazon.nova-lite-v1:0",
    "AGENT_LLM_PROVIDER=bedrock",
    f"S3_CACHE_BUCKET={bucket}",
    "S3_CACHE_PREFIX=market-data",
    f"COGNITO_USER_POOL_ID={q(os.environ.get('COGNITO_USER_POOL_ID', ''))}",
    f"COGNITO_CLIENT_ID={q(os.environ.get('COGNITO_CLIENT_ID', ''))}",
    f"COGNITO_REGION={q(os.environ.get('COGNITO_REGION', region))}",
    "AUTH_DISABLED=false",
]
secret = os.environ.get("SCHEDULER_SECRET") or ""
if secret and secret != "None":
    lines.append(f"RECON_SCHEDULER_SECRET={q(secret)}")
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
env_path.chmod(0o600)
PY
unset DB_URL SCHEDULER_SECRET
set -x

install -m 644 "${APP_ROOT}/infra/ec2_api/trade-recon-api.service" /etc/systemd/system/trade-recon-api.service
systemctl daemon-reload
systemctl enable trade-recon-api
systemctl restart trade-recon-api
