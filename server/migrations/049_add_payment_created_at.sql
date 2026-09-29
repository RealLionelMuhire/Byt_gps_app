-- Migration 049: record when a payment was initiated
--
-- payments had no created_at: verified_at was set when the payment was
-- initiated and then overwritten when it was confirmed (by the IntouchPay
-- webhook or by scripts/cron_expiry.py's reconciliation, up to 24h later).
-- That made it impossible to tell how long a pending payment has really
-- been waiting, and it moved revenue to whatever day the confirmation
-- happened to land on. created_at is now the initiation time (never
-- touched again); verified_at keeps meaning "when the status was last
-- resolved".
--
-- BACKFILL: existing rows get created_at = verified_at — the best value
-- available (exact for pending/failed rows, which never had verified_at
-- overwritten; the confirmation time for successful ones).
--
-- Purely additive. IDEMPOTENT — safe to re-run. Run with:
--   psql $DATABASE_URL -f migrations/049_add_payment_created_at.sql

BEGIN;

ALTER TABLE payments ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE;
UPDATE payments SET created_at = verified_at WHERE created_at IS NULL;
ALTER TABLE payments ALTER COLUMN created_at SET DEFAULT (now() AT TIME ZONE 'utc');
ALTER TABLE payments ALTER COLUMN created_at SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_payments_created_at ON payments (created_at);

COMMENT ON COLUMN payments.created_at IS 'When the payment was initiated (never updated). verified_at is when its status was last resolved.';

COMMIT;
