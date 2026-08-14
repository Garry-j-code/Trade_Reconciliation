# AGENTS.md

Trade reconciliation platform: two trade records (clearing broker + internal desk) matched deterministically; mismatches ("breaks") go to an AI agent that investigates and proposes resolutions, which a human approves. Full design record lives in `project_plan.md` — read it before starting non-trivial work. Update this file's "Current phase" section when the build order in `project_plan.md` §9 advances.

## Current phase

**Product hardening (step 9) + daily blotter / billing pause.** Cognito auth, CloudFront-only API edge, weekday **daily blotter** at 21:30 UTC (after US close). FastAPI on **t4g.micro** in the RDS VPC (`TradeReconEc2Api`). Postgres is reached over private IP (RDS SG: API SG + laptop `/32`, never `0.0.0.0/0`). No NAT Gateway. Details: `infra/README.md`.

## Stack

- Backend: Python, FastAPI, SQLAlchemy, pandas
- Database: Amazon RDS Postgres (`pgvector` for agent memory). Local code talks to RDS via `DATABASE_URL`. `docker compose` is emergency-only, not required. **Reuse** instance `trade-recon-postgres` — CDK must not create a second RDS.
- Agent: Amazon Bedrock Converse (`BEDROCK_MODEL_ID` as-is). **Current default is Amazon Nova Lite** `amazon.nova-lite-v1:0` (cost; no Anthropic use-case form). Claude comparison target remains Sonnet 4.5 via `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (cross-region inference profile, not the raw foundation-model id). IAM: `AmazonBedrockFullAccess` is fine for Nova; for Claude also need `bedrock:InvokeModel` (+ `InvokeModelWithResponseStream`) on both `arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-*` and `arn:aws:bedrock:us-east-1:*:inference-profile/us.anthropic.claude-sonnet-4-5-*` (plus `application-inference-profile` if you use a custom profile).
- Frontend: React, Recharts; production static host via CDK CloudFront + S3
- Infra: **AWS CDK (Python)** in `infra/` (Terraform not used). Account `894831047463`, profile `trade-recon-8948`, region `us-east-1`. Tag `Project=trade-recon`.
- Orchestration: EventBridge weekdays 21:30 UTC → SSM `daily-blotter` on EC2; daily stub memory writer; 30-day sunset watcher (stop EC2+RDS, disable rules)
- Market data S3 (reuse, do not destroy): `trade-recon-market-data-gagan-8948-us-east-1`

## Non-negotiable guardrails

- The deterministic pipeline (`backend/pipeline/`) never calls an LLM. It's pure functions over dataframes — matching, break detection, anything with a checkable right answer.
- The agent (`backend/agent/`) never does arithmetic reconciliation on money. It only reasons over what the pipeline already computed.
- The agent's tools (`backend/agent/tools.py`) are read-only and parameterized. Never add a free-form SQL tool.
- The agent writes only to `resolution_suggestions`. Never to `trades`, `matches`, or `breaks` directly — only the human-approval flow mutates those.
- `root_cause` and `suggested_action` are enums (`backend/agent/enums.py`), never free text. If a new category is genuinely needed, add it to the enum — don't let the model emit ad hoc strings.
- Every approval/override writes an `audit_log` row. Don't skip this to save a query.
- Cap agent tool calls at 5 per break.
- CDK: no second RDS, no NAT Gateway, never put `DATABASE_URL` passwords in source, never force-destroy the market-data bucket.

## Conventions

- Type hints on all Python function signatures.
- Every function in `backend/pipeline/` gets a unit test in `backend/tests/` — this is the layer that must never silently break.
- Break types to cover in tests: missing trade, quantity break, price break, duplicate, settlement date mismatch, split fill, and a corporate-action-driven mismatch that should NOT be flagged.
- Agent skills live as `SKILL.md` files under `backend/agent/skills/<skill-name>/` — one skill, one concern, per `project_plan.md` §6.2. Don't merge skills together to save a file.
- Market data is fetched once and cached to S3 as Parquet (`backend/data/fetch_market_data.py`). Nothing in the pipeline or agent should make a live external API call — if you're adding one, stop and check `project_plan.md` §3 first.
- Synthetic trades are generated only from that cache (`backend/data/generator.py`) — never from live market APIs.
- Canonical trade schema lives in `backend/pipeline/normalize.py` (`CANONICAL_COLUMNS`); SQLAlchemy models in `backend/db/models.py`.

## Commands

- Backend tests: `uv run pytest`
- Fetch market data: `uv run fetch-market-data` (or `uv run python -m backend.data.fetch_market_data`). Daily: `--lookback-days 5` (incremental merge; `--force` overwrites; `--skip-cached` skips nonempty files).
- Generate synthetic trades: `uv run generate-trades --trade-date YYYY-MM-DD` (default last US session; `--all-history` samples the 2y cache). Same `--seed` + date is idempotent.
- Normalize trades: `uv run normalize-trades --trade-date YYYY-MM-DD` (replace that session only in Parquet/DB)
- Match trades: `uv run match-trades` (full-book rematch; does **not** delete `audit_log`)
- Daily blotter (fetch optional + generate + one-day ingest + rematch): `uv run daily-blotter` / `--backfill-sessions 20 --skip-fetch`
- Pause billing: `uv run stop-billing` or `infra/scripts/stop-product.sh` (stop EC2 + RDS, disable rules). Resume: `uv run start-product` / `infra/scripts/start-product.sh`. Stopped RDS **still bills storage** and AWS may auto-start it after 7 days.
- Postgres: set `DATABASE_URL` to the RDS instance (see `.env.example`). Local `docker compose` is emergency-only.
- Sync deps: `uv sync --group dev`
- Investigate breaks: `uv run investigate-breaks` (`--limit N`, `--break-id`, `--provider stub|bedrock`)
- Write agent memory: `uv run write-agent-memory` (`--provider stub`, `--no-semantic`)
- Backend dev server: `uv run serve-api` or `uv run uvicorn backend.api.main:app --reload`
- Frontend: `cd frontend && npm install && npm run dev` (http://localhost:5173; proxies `/api` to `http://127.0.0.1:8000`, or set `VITE_API_BASE`)
- Deploy (product stacks): see `infra/README.md`
  - `cd infra && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
  - `cdk synth -c deployFrontendAssets=false`
  - Frontend: `cd frontend && npm ci && npm run build` then `cdk deploy TradeReconAuth TradeReconEc2Api TradeReconFrontend TradeReconPipeline` (`AWS_PROFILE=trade-recon-8948`)
  - Hosted API: `uv run python infra/ec2_api/put_database_url.py` then deploy as above (`enableEc2Api` / `enableAuth` default true)
  - Optional API Gateway stub: `cdk deploy TradeReconApi -c enableApi=true`
  - Tear down: `cdk destroy TradeReconFrontend TradeReconEc2Api TradeReconPipeline TradeReconAuth --force`

## What not to touch without asking

- The output contract in `project_plan.md` §6.3 (the agent's JSON schema) — downstream dashboard code depends on its shape.
- The human-approval gate logic — this is the one place where a "helpful" shortcut (e.g., auto-approving high-confidence suggestions without the confirmed routing rule) would misrepresent how the system actually works.
