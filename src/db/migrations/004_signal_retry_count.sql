-- Signal retry tracking for transient email processing failures
-- Run automatically on startup via migrator

ALTER TABLE signals ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
