#!/usr/bin/env bash
# Resume compute: start RDS, start EC2, re-enable EventBridge rules.
# RDS start often takes several minutes.
set -euo pipefail
export AWS_PROFILE="${AWS_PROFILE:-trade-recon-8948}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
uv run start-product
