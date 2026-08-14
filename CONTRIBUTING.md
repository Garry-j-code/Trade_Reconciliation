# Contributing

Default branch is **`main`** (stable). Work on a **feature branch** and open a pull request. There is no long-lived `develop` branch.

## Setup

```bash
uv sync --group dev
cp .env.example .env
# Fill MASSIVE_API_KEY / DATABASE_URL locally — never commit .env
```

Frontend: `cd frontend && npm install && npm run dev`.

## Before you open a PR

- `uv run pytest` (unit tests; no RDS, no API keys)
- Type hints on new Python functions
- Pipeline changes (`backend/pipeline/`) need tests in `backend/tests/`
- Do not commit `.env`, `*.pem`, RDS URLs with passwords, parquet caches, or `frontend/node_modules`

## Design guardrails

See `AGENTS.md` and `project_plan.md`. In short:

- The deterministic pipeline never calls an LLM.
- The agent never does money arithmetic and never writes to `trades` / `matches` / `breaks`.
- Agent tools stay read-only and parameterized (no free-form SQL).
- Approvals always write `audit_log`.

## Secrets and AWS

Do not put `DATABASE_URL`, Cognito passwords, or SSM values in source, issues, or PR bodies. CDK must not create a second RDS or a NAT Gateway. Reuse the existing market-data bucket; do not force-destroy it.
