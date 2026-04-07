-- Multi-Sleeve Investing Automation Platform
-- Database Schema for Supabase Postgres
--
-- Run this in Supabase SQL Editor to initialize the schema

-- Enable UUID extension (usually enabled by default in Supabase)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- SLEEVE CONFIGURATIONS
-- ============================================================================
-- Each sleeve represents a distinct investment strategy/source (e.g., Bravos)

CREATE TABLE sleeves (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,
    adapter_type TEXT NOT NULL,        -- 'bravos_web', 'api', 'email_parse'
    enabled BOOLEAN DEFAULT true,

    -- Capital allocation
    allocation_mode TEXT NOT NULL,     -- 'fixed_dollars', 'percent_of_equity', 'unit_based'
    allocation_value DECIMAL(12,4),    -- e.g., 10000.00 or 0.25
    unit_size DECIMAL(10,2) DEFAULT 500.00,  -- Dollar amount per weight unit (e.g., $500)
    cash_handling TEXT DEFAULT 'sleeve_isolated',  -- 'sleeve_isolated', 'shared_pool'
    rebalance_priority INT DEFAULT 100, -- Lower = higher priority

    approval_required BOOLEAN DEFAULT true,
    config JSONB DEFAULT '{}',         -- Adapter-specific settings

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- SLEEVE POSITIONS (Virtual Ledger)
-- ============================================================================
-- Tracks what positions belong to each sleeve. This is the source of truth
-- for sleeve composition, NOT the broker holdings.
-- Shares-based tracking: shares don't drift, only change on actual trades.

CREATE TABLE sleeve_positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sleeve_id UUID NOT NULL REFERENCES sleeves(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,

    -- Position tracking
    shares DECIMAL(16,6) NOT NULL DEFAULT 0,  -- Actual shares held for this sleeve
    weight DECIMAL(8,4),                       -- Source weight (e.g., Bravos weight 5, not 5%)
    cost_basis DECIMAL(12,2),                  -- Total cost basis for tax tracking

    -- Audit trail
    last_trade_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),

    -- One position per symbol per sleeve
    UNIQUE(sleeve_id, symbol)
);

-- Index for looking up positions by sleeve
CREATE INDEX idx_sleeve_positions_sleeve ON sleeve_positions(sleeve_id);

-- Auto-update timestamp
CREATE TRIGGER update_sleeve_positions_updated_at
    BEFORE UPDATE ON sleeve_positions
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- SIGNALS (Trigger Events)
-- ============================================================================
-- Raw events detected from sources (email notifications, scheduled checks, etc.)
-- A signal is "we detected something happened" - it triggers further processing

CREATE TABLE signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sleeve_id UUID NOT NULL REFERENCES sleeves(id) ON DELETE CASCADE,
    source_event_id TEXT NOT NULL,     -- Idempotency key (e.g., email message ID)
    event_type TEXT NOT NULL,          -- 'email_detected', 'scheduled_check', 'manual_trigger'
    detected_at TIMESTAMPTZ NOT NULL,
    raw_payload JSONB,                 -- Original data for debugging/audit

    status TEXT DEFAULT 'pending',     -- 'pending', 'processing', 'processed', 'failed', 'skipped'
    processed_at TIMESTAMPTZ,
    error_message TEXT,

    created_at TIMESTAMPTZ DEFAULT now(),

    -- Idempotency: one signal per (sleeve, source_event_id)
    UNIQUE(sleeve_id, source_event_id)
);

-- ============================================================================
-- PORTFOLIO INTENTS (Interpreted Desired State)
-- ============================================================================
-- The interpreted target state for a sleeve after processing a signal
-- "Here's what the sleeve should look like"

CREATE TABLE portfolio_intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id UUID NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
    sleeve_id UUID NOT NULL REFERENCES sleeves(id) ON DELETE CASCADE,

    target_allocations JSONB NOT NULL, -- [{symbol, target_weight, side}, ...]
    intent_type TEXT NOT NULL,         -- 'full_rebalance', 'partial_update'
    confidence DECIMAL(3,2),           -- 0.00 to 1.00

    -- Manual review workflow
    requires_review BOOLEAN DEFAULT false,
    review_reason TEXT,                -- Why review is needed
    review_status TEXT,                -- 'pending_review', 'approved', 'rejected', NULL
    reviewed_at TIMESTAMPTZ,
    reviewed_by TEXT,                  -- Telegram user ID or identifier

    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- RECONCILIATIONS (Computed Trade Deltas)
-- ============================================================================
-- The computed trades needed to move from current holdings to target intent
-- "Here's how to get there"

CREATE TABLE reconciliations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intent_id UUID NOT NULL REFERENCES portfolio_intents(id) ON DELETE CASCADE,
    sleeve_id UUID NOT NULL REFERENCES sleeves(id) ON DELETE CASCADE,

    holdings_snapshot JSONB NOT NULL,  -- Broker state at reconciliation time
    proposed_trades JSONB NOT NULL,    -- [{symbol, side, notional, quantity, rationale}, ...]

    result_type TEXT NOT NULL,         -- 'no_action', 'proposed', 'manual_review'
    review_reason TEXT,                -- If manual_review, why?

    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- APPROVALS (Human Authorization)
-- ============================================================================
-- Pending/completed approval requests for proposed trades

CREATE TABLE approvals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reconciliation_id UUID NOT NULL REFERENCES reconciliations(id) ON DELETE CASCADE,

    approval_code TEXT UNIQUE NOT NULL, -- Short code for Telegram (e.g., 'a3f2')
    proposed_trades JSONB NOT NULL,     -- Copy of trades for display
    telegram_message_id TEXT,           -- For updating the message later

    status TEXT DEFAULT 'pending',      -- 'pending', 'approved', 'rejected', 'expired'
    requested_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    responded_at TIMESTAMPTZ,
    approved_by TEXT,                   -- Telegram user ID

    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- EXECUTIONS (Order Tracking)
-- ============================================================================
-- Individual order executions, one per symbol/side

CREATE TABLE executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_id UUID NOT NULL REFERENCES approvals(id) ON DELETE CASCADE,

    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                 -- 'buy', 'sell'
    quantity DECIMAL(16,6),
    notional DECIMAL(12,2),

    -- Idempotency at order level - prevents duplicate orders on retry
    execution_key TEXT UNIQUE NOT NULL, -- hash(approval_id, symbol, side)

    broker_order_id TEXT,               -- Robinhood order ID
    status TEXT NOT NULL,               -- 'pending', 'submitted', 'filled', 'partial', 'failed', 'cancelled'
    broker_response JSONB,              -- Full response from broker for audit

    executed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- PORTFOLIO SNAPSHOTS (Drift Detection)
-- ============================================================================
-- Point-in-time snapshots of portfolio state for drift detection

CREATE TABLE snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL,               -- 'robinhood', 'bravos', etc.
    sleeve_id UUID REFERENCES sleeves(id) ON DELETE SET NULL,  -- NULL for broker-wide snapshots
    data JSONB NOT NULL,                -- Full portfolio data
    taken_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- IDEMPOTENCY KEYS (General Purpose)
-- ============================================================================
-- Track processed operations to prevent duplicates

CREATE TABLE idempotency_keys (
    key TEXT PRIMARY KEY,
    scope TEXT NOT NULL,               -- 'signal', 'intent', 'reconciliation', 'execution'
    result JSONB,                      -- Cached result if needed
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ
);

-- ============================================================================
-- INDEXES
-- ============================================================================

-- Signals: lookup by sleeve and status for processing
CREATE INDEX idx_signals_sleeve_status ON signals(sleeve_id, status);
CREATE INDEX idx_signals_detected_at ON signals(detected_at DESC);

-- Intents: find items needing review
CREATE INDEX idx_intents_review ON portfolio_intents(requires_review, review_status)
    WHERE requires_review = true;

-- Approvals: find pending/expiring approvals
CREATE INDEX idx_approvals_status_expires ON approvals(status, expires_at)
    WHERE status = 'pending';

-- Executions: track order status
CREATE INDEX idx_executions_status ON executions(status);
CREATE INDEX idx_executions_approval ON executions(approval_id);

-- Snapshots: find recent snapshots by source
CREATE INDEX idx_snapshots_source_taken ON snapshots(source, taken_at DESC);

-- Idempotency: cleanup expired keys
CREATE INDEX idx_idempotency_expires ON idempotency_keys(expires_at)
    WHERE expires_at IS NOT NULL;

-- ============================================================================
-- FUNCTIONS
-- ============================================================================

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply to sleeves table
CREATE TRIGGER update_sleeves_updated_at
    BEFORE UPDATE ON sleeves
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- INITIAL DATA
-- ============================================================================

-- Insert Bravos as the first sleeve (unit-based allocation: $500 per weight unit)
INSERT INTO sleeves (name, adapter_type, allocation_mode, unit_size, config)
VALUES (
    'bravos',
    'bravos_web',
    'unit_based',
    500.00,
    '{"poll_interval_minutes": 5, "email_from": "bravos"}'::jsonb
);

-- Insert Buffett sleeve (unit-based allocation: $240 per weight unit based on 13F weights)
INSERT INTO sleeves (name, adapter_type, allocation_mode, unit_size, config)
VALUES (
    'buffett',
    'sec_13f',
    'unit_based',
    240.00,
    '{"cik": "0001067983", "top_n_positions": 10, "min_portfolio_weight_pct": 3.0}'::jsonb
);

-- ============================================================================
-- ROW LEVEL SECURITY (Optional - enable if using Supabase Auth)
-- ============================================================================
-- Uncomment these if you want to use Supabase's RLS features

-- ALTER TABLE sleeves ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE signals ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE portfolio_intents ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE reconciliations ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE executions ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE snapshots ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;

-- ============================================================================
-- COMMENTS
-- ============================================================================

COMMENT ON TABLE sleeves IS 'Investment strategy sources (e.g., Bravos, other newsletters)';
COMMENT ON TABLE signals IS 'Trigger events detected from sources (emails, scheduled checks)';
COMMENT ON TABLE portfolio_intents IS 'Interpreted target portfolio state for a sleeve';
COMMENT ON TABLE reconciliations IS 'Computed trade deltas to reach target state';
COMMENT ON TABLE approvals IS 'Human authorization requests for proposed trades';
COMMENT ON TABLE executions IS 'Individual order executions with broker';
COMMENT ON TABLE snapshots IS 'Point-in-time portfolio snapshots for drift detection';
COMMENT ON TABLE idempotency_keys IS 'Deduplication tracking for operations';
