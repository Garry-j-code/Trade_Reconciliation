#!/usr/bin/env bash
# Pause compute: disable EventBridge rules, stop EC2, stop RDS.
# Does not delete the market-data bucket or RDS. Storage still bills on a stopped RDS.
set -euo pipefail
export AWS_PROFILE="${AWS_PROFILE:-trade-recon-8948}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
uv run stop-billing
