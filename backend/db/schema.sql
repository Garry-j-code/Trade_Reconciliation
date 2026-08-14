-- Trade Reconciliation — Postgres schema (project_plan.md §4)
-- Applied automatically via SQLAlchemy create_all when DATABASE_URL is set.
-- Local: docker compose up -d  (pgvector/pgvector image)
--
-- Canonical normalized_trades columns (see backend/pipeline/normalize.py):
--   trade_id, source (broker|desk), symbol, trade_date, settlement_date,
--   side, quantity, price, currency, account, executing_party, pair_id,
--   raw_payload

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Raw broker (generator BROKER_COLUMNS, untouched)
CREATE TABLE IF NOT EXISTS raw_broker_trades (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    broker_trade_id TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    trade_date      DATE NOT NULL,
    settlement_date DATE NOT NULL,
    side            TEXT NOT NULL,
    quantity        DOUBLE PRECISION NOT NULL,
    price           DOUBLE PRECISION NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    account_id      TEXT NOT NULL,
    execution_venue TEXT NOT NULL,
    pair_id         TEXT,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Raw desk (generator DESK_COLUMNS, untouched)
CREATE TABLE IF NOT EXISTS raw_desk_trades (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    blotter_id  TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    trade_date  DATE NOT NULL,
    settle_date DATE NOT NULL,
    side        TEXT NOT NULL,
    qty         DOUBLE PRECISION NOT NULL,
    px          DOUBLE PRECISION NOT NULL,
    ccy         TEXT NOT NULL DEFAULT 'USD',
    desk_code   TEXT NOT NULL,
    trader      TEXT NOT NULL,
    pair_id     TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS normalized_trades (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id        TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('broker', 'desk')),
    symbol          TEXT NOT NULL,
    trade_date      DATE NOT NULL,
    settlement_date DATE NOT NULL,
    side            TEXT NOT NULL,
    quantity        DOUBLE PRECISION NOT NULL,
    price           DOUBLE PRECISION NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    account         TEXT NOT NULL,
    executing_party TEXT NOT NULL,
    pair_id         TEXT,
    raw_payload     JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, trade_id)
);

CREATE TABLE IF NOT EXISTS matches (
    match_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    broker_trade_id TEXT NOT NULL,
    desk_trade_id   TEXT NOT NULL,
    pair_id         TEXT,
    match_pass      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS breaks (
    break_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    break_type       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open',
    pair_id          TEXT,
    broker_trade_ids TEXT,
    desk_trade_ids   TEXT,
    symbol           TEXT,
    trade_date       DATE,
    detail           JSONB,
    cluster_id       UUID,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- root_cause / suggested_action: pinned enums in backend/agent/enums.py
CREATE TABLE IF NOT EXISTS resolution_suggestions (
    suggestion_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    break_id         UUID NOT NULL REFERENCES breaks(break_id) ON DELETE CASCADE,
    root_cause       TEXT NOT NULL,
    confidence       DOUBLE PRECISION NOT NULL,
    explanation      TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    evidence         JSONB,
    inferred         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Existing RDS databases created before step 6:
-- ALTER TABLE resolution_suggestions ADD COLUMN IF NOT EXISTS inferred BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- SET NULL so rematching (delete/replace breaks) does not wipe HITL history.
    -- Existing DBs: see ensure_audit_log_survives_break_delete() in session.py.
    break_id                   UUID REFERENCES breaks(break_id) ON DELETE SET NULL,
    suggestion_id              UUID REFERENCES resolution_suggestions(suggestion_id) ON DELETE SET NULL,
    actor                      TEXT NOT NULL,
    action                     TEXT NOT NULL,
    override_note              TEXT,
    agent_suggestion_snapshot  JSONB,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_memory (
    memory_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope            TEXT NOT NULL,
    memory_type      TEXT NOT NULL,
    content          TEXT NOT NULL,
    embedding        VECTOR(1536),
    source_break_ids UUID[],
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
