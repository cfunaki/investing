DELETE FROM schema_migrations WHERE filename = '004_signal_retry_count.sql';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
INSERT INTO schema_migrations (filename) VALUES ('004_signal_retry_count.sql');
