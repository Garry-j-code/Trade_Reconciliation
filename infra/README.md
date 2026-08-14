# AWS CDK — Trade Reconciliation

Python CDK app. **IaC choice: CDK** (not Terraform).

| Account | Profile | Region |
|---|---|---|
| `894831047463` | `trade-recon-8948` | `us-east-1` |

## What deploys by default

| Stack | Resources | Cost notes |
|---|---|---|
| `TradeReconAuth` | Cognito user pool + app client + generated demo password (Secrets Manager + SSM) | Cognito MAU free tier; ~$0.40/mo per secret |
| `TradeReconFrontend` | Dedicated S3 bucket + CloudFront (OAC); `/api/*` and `/health` origin to EC2; `config.json` with Cognito IDs | Pennies/month; **does not** touch the market-data bucket |
| `TradeReconEc2Api` | t4g.micro in a **public subnet of the RDS VPC**, Elastic IP, API SG, scheduler secret | ~$6/mo on-demand; **no NAT Gateway** |
| `TradeReconPipeline` | Standard Step Functions (one Task) + trigger Lambda + EventBridge rules | Near-zero at this volume |

**Not created:** second RDS, NAT Gateway, custom domain.

**Reused (reference only):**

- S3: `trade-recon-market-data-gagan-8948-us-east-1`
- RDS: `trade-recon-postgres` (SG: 5432 from API SG + laptop `/32` only — **never** `0.0.0.0/0`)

## Security posture

- **CloudFront** is the public HTTPS entry (`https://d1a8rtzx54qkw.cloudfront.net`).
- EC2 **port 80** allows the managed prefix list `com.amazonaws.global.cloudfront.origin-facing` (`pl-3b927c52` in us-east-1), not `0.0.0.0/0`.
- FastAPI verifies Cognito JWTs on all `/api/*`. **`/health` is public** for ops.
- Scheduler `POST /api/recon/run` uses header `X-Recon-Scheduler-Secret` (Secrets Manager); the UI Run button uses a logged-in Bearer token.
- RDS stays private-SG-only. `DATABASE_URL` is SSM `/trade-recon/database-url` (never printed by deploy scripts, never in git).

## Hosted API (`enableEc2Api`, default **true**)

FastAPI runs on one **t4g.micro** in the same VPC as RDS (`vpc-0f92428efe0f6e0c1`, public subnet `subnet-03c8c10c72fed25b4` / us-east-1a):

- CloudFront talks HTTPS to viewers and **HTTP** to this origin.
- RDS on **private IP**. Instance role: SSM Session Manager, Bedrock Nova Lite, S3 market-data read, Secrets Manager (scheduler secret), CloudWatch logs.
- EC2 reaches Bedrock/S3 via the **Internet Gateway** (no NAT).

Store the DB URL (does not print it):

```bash
export AWS_PROFILE=trade-recon-8948
uv run python infra/ec2_api/put_database_url.py
```

Then:

```bash
cd frontend && npm ci && npm run build && cd ../infra
source .venv/bin/activate
cdk deploy TradeReconAuth TradeReconEc2Api TradeReconFrontend TradeReconPipeline --require-approval never
```

**Public HTTPS:** `https://d1a8rtzx54qkw.cloudfront.net` (login) and `/health`.

Demo analyst username: `analyst@traderecon.demo`  
Password (change after first login):

```bash
aws ssm get-parameter --name /trade-recon/demo-analyst-password --with-decryption --query Parameter.Value --output text
```

The React app uses relative `/api` and `/health`. **Do not** set `VITE_API_BASE` when using CloudFront. Cognito IDs are written to `config.json` at deploy time.

## Schedules

| When | What |
|---|---|
| Weekdays 13:00 UTC | EventBridge → Step Functions → Lambda → `POST /api/recon/run` through CloudFront |
| Daily 07:00 UTC | EventBridge → Lambda → SSM Run Command `write-agent-memory --provider stub --no-semantic` |

Manual recon: UI **Run reconciliation** (Cognito) or:

```bash
aws stepfunctions start-execution \
  --state-machine-arn "$STATE_MACHINE_ARN" \
  --input '{"action":"recon"}'
```

## Optional API Gateway stub (`enableApi`)

Default **`enableApi=false`**. Legacy HTTP API + health-stub Lambda. Prefer `TradeReconEc2Api`.

## Prerequisites

- Node.js (for `aws-cdk` CLI)
- Python 3.11+ (3.12 recommended for CDK)
- AWS credentials: `export AWS_PROFILE=trade-recon-8948`
- Frontend build before deploying the frontend stack with assets

## Bootstrap (once per account/region)

```bash
export AWS_PROFILE=trade-recon-8948
export CDK_DEFAULT_ACCOUNT=894831047463
export CDK_DEFAULT_REGION=us-east-1

cd infra
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install --no-save aws-cdk@2

npx aws-cdk bootstrap aws://894831047463/us-east-1
```

## Synth (no AWS changes)

```bash
cd infra
source .venv/bin/activate
cdk synth -c deployFrontendAssets=false
```

## Tear down

```bash
export AWS_PROFILE=trade-recon-8948
cd infra && source .venv/bin/activate
cdk destroy TradeReconFrontend TradeReconEc2Api TradeReconPipeline TradeReconAuth --force
# If you enabled the Lambda stub:
# cdk destroy TradeReconApi -c enableApi=true --force
```

Destroying `TradeReconEc2Api` removes the extra RDS SG rule from the API SG; it does **not** destroy RDS or the market-data bucket. The laptop `/32` rule is left in place.

Frontend bucket has `RemovalPolicy.DESTROY` + auto-delete objects. **Market-data bucket and RDS are not in these stacks** and will not be destroyed.

## Tags

All stacks tag resources `Project=trade-recon`.
