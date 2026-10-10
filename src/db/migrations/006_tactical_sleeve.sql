-- 006: create the 'tactical' sleeve, replacing 'bravos' as the
-- discretionary satellite sleeve.
--
-- The tactical sleeve holds positions from trade requests submitted by the
-- AI assistant (POST /admin/sleeve/tactical/custom-buys|custom-sells),
-- each still requiring the owner's Telegram approval before execution.
-- It is capped at tactical_sleeve_max_pct (default 10%) of portfolio net
-- equity, enforced in the custom-buys endpoint.
--
-- The 'bravos' sleeve is NOT deleted here: it is wound down first via the
-- approved rebalance trades, then disabled with:
--   UPDATE sleeves SET enabled = false WHERE name = 'bravos';

INSERT INTO sleeves (name, adapter_type, allocation_mode, unit_size, config)
SELECT
    'tactical',
    'manual',
    'unit_based',
    500.00,
    '{"description": "Tactical satellite sleeve: AI-submitted trade requests, owner-approved in Telegram. Capped at 10% of net equity."}'::jsonb
WHERE NOT EXISTS (SELECT 1 FROM sleeves WHERE name = 'tactical');
