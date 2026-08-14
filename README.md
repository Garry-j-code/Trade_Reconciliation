# Trade Reconciliation

Client-style ops product for **two-sided trade reconciliation**: a clearing-broker file and an internal desk blotter are matched with a deterministic pipeline. Mismatches (**breaks**) go to an AI investigator that proposes a resolution; a human always approves before books change.

This is a full product surface — FastAPI + React console, Cognito login, weekday recon, Bedrock agent — not a notebook demo. Trades are **synthetic** (no firm publishes internal blotters) but generated against **real** market data: prices, volumes, calendars, and corporate actions. Split-driven quantity mismatches are real, not injected.

**Hosted console:** `https://d1a8rtzx54qkw.cloudfront.net` (Cognito). **Repo is private** so infra identifiers stay off the public internet; make it public later if you want a portfolio link.

## Current phase

**Product hardening.** CloudFront + Cognito, FastAPI on a **t4g.micro** in the RDS VPC, weekday EventBridge recon. IaC is **AWS CDK (Python)** in [`infra/`](infra/README.md).

Large Parquet caches (`backend/data/cache/`, generated/normalized/matched trades) are **gitignored**. Re-fetch or restore from S3 (`trade-recon-market-data-gagan-8948-us-east-1`); do not commit bars.

## Setup

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
uv sync --group dev
cp .env.example .env
# Edit .env and set MASSIVE_API_KEY (or POLYGON_API_KEY — both work)
```

Get a free Massive (formerly Polygon.io) API key at [massive.com](https://massive.com).

## Fetch & cache market data

Fetches EOD daily bars, splits, dividends, and upcoming market holidays from Massive; writes Parquet under `backend/data/cache/`; spot-checks split dates with yfinance (Massive remains source of record).

```bash
# Console script
uv run fetch-market-data

# Or module form
uv run python -m backend.data.fetch_market_data

# Subset / options
uv run fetch-market-data --symbols AAPL MSFT --lookback-days 365 -v
uv run fetch-market-data --skip-yfinance
```

Cache layout:

```
backend/data/cache/
  bars/{SYMBOL}.parquet
  splits.parquet
  dividends.parquet
  calendar.parquet
  cross_check/splits_report.json
  cross_check/splits_report.csv
```

### Optional S3 upload

After writing the local Parquet cache, `fetch-market-data` can upload it to S3 if `S3_CACHE_BUCKET` is set. Credentials come from the normal AWS chain (`aws configure`, env vars, or an IAM role) — not from this project's Massive key.

1. Create a dedicated bucket (do not reuse unrelated app buckets):

```bash
export AWS_PROFILE=trade-recon-8948   # named profile for this account
aws s3 mb s3://trade-recon-market-data-<yourname>-8948-us-east-1 --region us-east-1
```

2. Add to `.env` (region should match the bucket):

```bash
S3_CACHE_BUCKET=trade-recon-market-data-<yourname>-8948-us-east-1
S3_CACHE_PREFIX=market-data
AWS_REGION=us-east-1
# AWS_PROFILE=trade-recon-8948
```

3. Verify identity + bucket access, then fetch (start small):

```bash
export AWS_PROFILE=trade-recon-8948
aws sts get-caller-identity
aws s3 ls s3://$S3_CACHE_BUCKET
uv run fetch-market-data --symbols AAPL --lookback-days 30 --skip-yfinance -v
aws s3 ls s3://$S3_CACHE_BUCKET/market-data/ --recursive
```

Minimum IAM on that bucket/prefix: `s3:CreateBucket` (once), then `s3:ListBucket`, `s3:PutObject`, `s3:GetObject`.

The pipeline and agent must read this cache only — they never call live market APIs.

## Generate synthetic trades

Builds clearing-broker and internal-desk legs from the **cached** Parquet (no live API). Injects non-corporate-action breaks at controlled rates; corporate-action quantity mismatches use **real split factors** from `splits.parquet` (broker adjusted, desk lag). Writes Parquet under `backend/data/generated/` plus a ground-truth manifest.

```bash
# After fetch-market-data has populated the cache
uv run generate-trades
uv run python -m backend.data.generator

# Smaller / reproducible run
uv run generate-trades --symbols AAPL MSFT --n-trades 100 --seed 42 -v
```

Output:

```
backend/data/generated/
  broker_trades.parquet
  desk_trades.parquet
  ground_truth.parquet
  generation_summary.json
```

## Normalize trades (canonical schema)

Maps broker + desk raw columns into one canonical frame (`source` = `broker` | `desk`). Writes Parquet under `backend/data/normalized/`. When `DATABASE_URL` is set (default: Amazon RDS), also loads raw + normalized rows into Postgres.

Canonical columns: `trade_id`, `source`, `symbol`, `trade_date`, `settlement_date`, `side`, `quantity`, `price`, `currency`, `account` (account_id | desk_code), `executing_party` (venue | trader), `pair_id`, `raw_payload`.

```bash
# After generate-trades
uv run normalize-trades
uv run python -m backend.pipeline.ingest

# Custom dirs / skip DB even if DATABASE_URL is set
uv run normalize-trades --input-dir backend/data/generated --output-dir backend/data/normalized --no-db
```

SQLAlchemy models + reference DDL: `backend/db/models.py`, `backend/db/schema.sql` (tables: `raw_broker_trades`, `raw_desk_trades`, `normalized_trades`, `matches`, `breaks`, plus empty-ready `resolution_suggestions`, `audit_log`, `agent_memory`).

## Match trades (exact → tolerance)

Compares normalized broker vs desk legs. **Exact** (symbol + side + trade_date + settlement + quantity + price), then **tolerance** (same key except price within **5 bps**), then **corporate-action** (qty/price ratio matches a cached split near `execution_date` — treated as a match, not a break), then **split-fill** (one desk block vs broker fills that sum to the desk qty). Leftovers become breaks: missing trade, quantity, price, duplicate, settlement-date mismatch.

Writes `backend/data/matched/matches.parquet` and `breaks.parquet`. When `DATABASE_URL` is set, also replaces RDS `matches` / `breaks`. Reads splits from the market-data cache only — no live API.

```bash
# After normalize-trades
uv run match-trades
uv run python -m backend.pipeline.matcher

# Parquet only / custom dirs
uv run match-trades --input-dir backend/data/normalized --output-dir backend/data/matched --no-db
```

### Postgres (Amazon RDS)

Local Python talks to RDS via `DATABASE_URL` in `.env` (see `.env.example`). A local database is not required. `docker compose` is an emergency-only fallback if RDS is unreachable.

```bash
# DATABASE_URL=postgresql+psycopg://recon:YOUR_RDS_PASSWORD@trade-recon-postgres.xxxxx.us-east-1.rds.amazonaws.com:5432/trade_recon
uv run normalize-trades   # writes Parquet and loads RDS
uv run match-trades       # writes matches/breaks Parquet and loads RDS
```

Core unit tests do not require a database. An optional integration test runs only when `DATABASE_URL` is set.

## Local API (FastAPI)

Runs on your machine and talks to RDS via `DATABASE_URL`. CloudFront hosts the static UI (CDK); API Gateway + Mangum packaging is Phase 2 (`infra/README.md`, `backend/api/lambda_handler.py`). Interactive docs: http://127.0.0.1:8000/docs

```bash
uv run serve-api
# or
uv run uvicorn backend.api.main:app --reload
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Process up; `db` is `connected` or `unavailable` |
| GET | `/api/summary` | Trade counts, % clean-matched, open breaks by type, notional at risk |
| GET | `/api/breaks` | Filterable list (`desk`, `symbol`, `break_type`, `date`) + pagination |
| GET | `/api/breaks/{id}` | Side-by-side broker vs desk + suggestion placeholder |
| GET | `/api/matches` | Optional match list |
| POST | `/api/recon/run` | Normalize + match local generated Parquet into RDS (no live Massive) |
| POST | `/api/breaks/{id}/approve` | Human approve (always writes `audit_log`; never auto-approves) |
| POST | `/api/breaks/{id}/reject` | Reject with note |
| POST | `/api/breaks/{id}/override` | Override/resolve with note |
| POST | `/api/breaks/{id}/investigate` | Agent investigate (writes `resolution_suggestions` only; cap 5 tools). Body: `{"provider":"stub"|"bedrock"}`. 503 if Bedrock is denied. |
| GET | `/api/breaks/{id}/suggestion` | Latest agent suggestion, or 404 if none |

`POST /api/recon/run` reads `backend/data/generated/*.parquet` and the local splits cache only. Cap via `RECON_TIMEOUT_SECONDS` (default 120).

## Agent (Bedrock, local Python)

The agent investigates pipeline-computed breaks and writes **only** `resolution_suggestions`. It never does money arithmetic and never writes to trades/matches/breaks. Tools are read-only and parameterized (no free-form SQL). Skills live under `backend/agent/skills/*/SKILL.md` and are concatenated into the system prompt.

Uses Amazon Bedrock Converse in `us-east-1` via the default boto3 chain (`AWS_PROFILE=trade-recon-8948`). **Current default** `BEDROCK_MODEL_ID` is **Amazon Nova Lite** `amazon.nova-lite-v1:0` (on-demand / in-region; cost-focused; no Anthropic use-case form). Same Converse + tool-use path as Claude. For later output comparison, switch to the **Sonnet 4.5 cross-region inference profile** `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (not the raw foundation-model id `anthropic.claude-sonnet-4-5-20250929-v1:0`, which raises `ResourceNotFoundException`). Tests and `--provider stub` never call Bedrock.

```bash
# After match-trades has written open breaks to RDS
export AWS_PROFILE=trade-recon-8948
uv run investigate-breaks --limit 5
uv run investigate-breaks --break-id <uuid> --provider stub
uv run write-agent-memory --provider stub --no-semantic
```

Clustering: similar open breaks (same `break_type` + symbol + trade date) share one investigation; siblings get a copy with `inferred=true`.

IAM for live Bedrock: `AmazonBedrockFullAccess` is enough for Nova Lite. For Claude Sonnet 4.5 comparison runs, grant `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on **both** the foundation model and the inference profile (and an application inference profile if you create one):

- `arn:aws:bedrock:*::foundation-model/amazon.nova-lite-*` (Nova default)
- `arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-*` (Claude comparison)
- `arn:aws:bedrock:us-east-1:*:inference-profile/us.anthropic.claude-sonnet-4-5-*`
- `arn:aws:bedrock:us-east-1:*:application-inference-profile/*` (only if you use a custom application profile)

Enable model access for Nova Lite (default) and, when comparing, Claude plus Titan Embeddings if you run the memory writer live. Anthropic use-case form is **not** required for Nova.

## Local dashboard (React + Vite)

Ops console at http://localhost:5173. Vite proxies `/api` and `/health` to the local API. CORS on the API also allows `http://localhost:5173` if you set `VITE_API_BASE=http://127.0.0.1:8000`.

```bash
# Terminal 1 — API (RDS via DATABASE_URL)
uv run serve-api

# Terminal 2 — UI
cd frontend
npm install
npm run dev
```

Pages: Dashboard (summary cards + break-type chart), Breaks (filters), Break detail (broker vs desk + agent panel, approve/reject, optional Investigate).

## Architecture (AWS vs local)

```mermaid
flowchart LR
  subgraph aws [AWS us-east-1]
    CF[CloudFront]
    S3UI[S3 dashboard]
    COG[Cognito]
    EC2[EC2 t4g.micro FastAPI]
    RDS[(RDS trade-recon-postgres)]
    MD[S3 market-data cache]
    EB[EventBridge]
    SFN[Step Functions]
    BR[Bedrock Nova Lite]
    CF --> S3UI
    CF --> EC2
    EC2 --> RDS
    EC2 --> MD
    EC2 --> BR
    EC2 --> COG
    EB --> SFN
    SFN --> EC2
  end
  Analyst[Analyst browser] --> CF
  Analyst --> COG
  subgraph laptop [Laptop optional]
    VITE[Vite :5173]
    UV[uvicorn :8000]
    VITE --> UV
    UV --> RDS
  end
```

CloudFront is the only public HTTPS entry. EC2:80 accepts the CloudFront origin-facing prefix list only (not `0.0.0.0/0`). RDS:5432 is the API security group plus a laptop `/32` — never public. There is no NAT Gateway and no second RDS.

## How a client operates

1. Open `https://d1a8rtzx54qkw.cloudfront.net` and sign in (email/password). Demo analyst is seeded by CDK; get the password from SSM `/trade-recon/demo-analyst-password` (SecureString) and **change it**.
2. Dashboard shows match rate, breaks by type, and notional at risk.
3. **Run reconciliation** (authenticated) normalizes and matches cached synthetic trades into RDS. Weekdays at **13:00 UTC** the same path runs via EventBridge → Step Functions.
4. Open a break, **Investigate** (agent writes `resolution_suggestions` only), then **Approve** or **Reject** with a note. Every decision writes `audit_log`.
5. Daily **07:00 UTC** a stub memory writer runs on the instance (SSM, `--provider stub`) so Bedrock is not billed for the schedule.

Local development: `AUTH_DISABLED=true` in `.env`, `uv run serve-api`, `cd frontend && npm run dev`. Hosted UI always requires Cognito.

## Cost (us-east-1, order-of-magnitude)

| Resource | Typical monthly | Notes |
|---|---|---|
| RDS `trade-recon-postgres` | existing instance (often ~$12–25) | **Reused**, not created by CDK |
| EC2 t4g.micro | ~$6 on-demand | Free-tier hours may apply |
| CloudFront + frontend S3 | pennies | Portfolio traffic |
| Cognito | $0 | 50k MAU free tier; one user |
| Step Functions + Lambda + EventBridge | ~$0 | Few standard transitions/week |
| Secrets Manager | ~$0.40 per secret | Demo password + scheduler secret |
| Bedrock Nova Lite | on-demand, only when investigating | Scheduled memory writer is **stub** |
| NAT Gateway | **$0** | Not deployed |
| Second RDS | **$0** | Not created |

### Stop billing

Tear down product stacks (does **not** destroy reused RDS or the market-data bucket):

```bash
export AWS_PROFILE=trade-recon-8948
cd infra && source .venv/bin/activate
cdk destroy TradeReconFrontend TradeReconEc2Api TradeReconPipeline TradeReconAuth --force
```

To **pause** without destroying stacks, `uv run python -m backend.ops.billing stop` disables EventBridge rules and stops EC2 + RDS. Stopped RDS still bills storage and AWS **auto-restarts** a stopped instance after 7 days — destroy or snapshot+delete if you need a hard stop. There is **no NAT Gateway** to forget.

## Deploy (AWS CDK)

IaC is **CDK (Python)** in `infra/` — not Terraform. Reuses RDS `trade-recon-postgres` and the market-data bucket; does **not** create a second RDS, a NAT Gateway, or open Postgres to `0.0.0.0/0`.

```bash
export AWS_PROFILE=trade-recon-8948
uv run python infra/ec2_api/put_database_url.py   # SSM only; does not print the URL

cd frontend && npm ci && npm run build && cd ../infra
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk deploy TradeReconAuth TradeReconEc2Api TradeReconFrontend TradeReconPipeline --require-approval never
```

Login URL: `https://d1a8rtzx54qkw.cloudfront.net`  
Health (public): `https://d1a8rtzx54qkw.cloudfront.net/health`  
Demo password (once): `aws ssm get-parameter --name /trade-recon/demo-analyst-password --with-decryption --query Parameter.Value --output text`

Tear down (does **not** destroy RDS or the market-data bucket):

```bash
cdk destroy TradeReconFrontend TradeReconEc2Api TradeReconPipeline TradeReconAuth --force
```

Full commands and security notes: [`infra/README.md`](infra/README.md).

## Security notes

- Cognito issues JWTs; FastAPI rejects unauthenticated `/api/*` with 401. `/health` is public for ops.
- RDS is not on `0.0.0.0/0`. `DATABASE_URL` lives in SSM, not in git or CDK source.
- EC2 HTTP is not open to the whole internet; use CloudFront.
- One demo user, not a multi-tenant IdP. Rotate the seeded password after handover.

## Tests

Unit tests mock HTTP / yfinance and use on-disk fixtures for the generator / normalizer; no API key or Postgres required:

```bash
uv run pytest
```

## Docs

- `project_plan.md` — full design
- `AGENTS.md` — operational guardrails for coding agents
