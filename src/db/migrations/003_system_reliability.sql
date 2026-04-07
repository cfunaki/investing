-- System Reliability: state persistence, entry prices, queued executions
-- Run via Supabase SQL Editor

CREATE TABLE IF NOT EXISTS key_value_state (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entry_prices (
    symbol TEXT PRIMARY KEY,
    price NUMERIC NOT NULL,
    source TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS queued_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_id UUID NOT NULL REFERENCES approvals(id),
    trades JSONB NOT NULL,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    execute_after TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    executed_at TIMESTAMPTZ,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_queued_executions_status
    ON queued_executions(status, execute_after);

-- Prevent duplicate queuing of the same approval
CREATE UNIQUE INDEX IF NOT EXISTS idx_queued_executions_approval_pending
    ON queued_executions(approval_id) WHERE status = 'pending';

-- Seed entry_prices from known values
INSERT INTO entry_prices (symbol, price, source) VALUES
    ('ALUM', 3.85, 'cache_import'),
    ('EME', 687.73, 'cache_import'),
    ('D', 59.5, 'cache_import'),
    ('FHI', 54.42, 'cache_import'),
    ('DBC', 24.76, 'cache_import'),
    ('HSY', 211.75, 'cache_import'),
    ('NTR', 66.5, 'cache_import'),
    ('AA', 56.7, 'cache_import'),
    ('EXC', 49.82, 'cache_import'),
    ('ANDE', 58.5, 'cache_import'),
    ('CENX', 55.5, 'cache_import'),
    ('NEE', 92.2, 'cache_import'),
    ('LIN', 487, 'cache_import')
ON CONFLICT (symbol) DO NOTHING;
