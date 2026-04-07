-- Migration: Add sleeve_positions table for virtual ledger
-- Run this in Supabase SQL Editor

-- ============================================================================
-- SLEEVE POSITIONS (Virtual Ledger)
-- ============================================================================
-- Tracks what positions belong to each sleeve. This is the source of truth
-- for sleeve composition, NOT the broker holdings.
-- Shares-based tracking: shares don't drift, only change on actual trades.

CREATE TABLE IF NOT EXISTS sleeve_positions (
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
CREATE INDEX IF NOT EXISTS idx_sleeve_positions_sleeve ON sleeve_positions(sleeve_id);

-- Auto-update timestamp trigger (reuse existing function)
DROP TRIGGER IF EXISTS update_sleeve_positions_updated_at ON sleeve_positions;
CREATE TRIGGER update_sleeve_positions_updated_at
    BEFORE UPDATE ON sleeve_positions
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Add unit_size column to sleeves if it doesn't exist
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'sleeves' AND column_name = 'unit_size'
    ) THEN
        ALTER TABLE sleeves ADD COLUMN unit_size DECIMAL(10,2) DEFAULT 500.00;
    END IF;
END $$;

-- Update Bravos sleeve to use unit_based allocation
UPDATE sleeves
SET allocation_mode = 'unit_based', unit_size = 500.00
WHERE name = 'bravos';

-- Add comment
COMMENT ON TABLE sleeve_positions IS 'Virtual ledger tracking positions per sleeve. Source of truth for sleeve composition.';
