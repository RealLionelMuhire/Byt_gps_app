-- Migration 051: subscriptions.is_recurring
--
-- False (default) = one-time subscription that ends at expires_at;
-- True = recurring subscription that auto-renews and is never flipped by
-- scripts/cron_expiry.py (only reminders are sent for it).
--
-- BACKFILL: every existing subscription is treated as one-time, since none
-- of them auto-renew today.
--
-- Purely additive. IDEMPOTENT — safe to re-run. Run with:
--   psql $DATABASE_URL -f migrations/051_add_is_recurring.sql

BEGIN;

ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS is_recurring BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN subscriptions.is_recurring IS 'TRUE = auto-renews, never expired by cron; FALSE = one-time, completed at expires_at.';

COMMIT;
