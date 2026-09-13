-- Migration 036: Track whether the "expiring soon" reminder has been sent
-- Run with: psql $DATABASE_URL -f migrations/036_add_subscription_expiry_reminder.sql

ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS expiry_reminder_sent_at TIMESTAMP;

COMMENT ON COLUMN subscriptions.expiry_reminder_sent_at IS
    'Set once the "expiring soon" push+email has been sent for this subscription, so scripts/cron_expiry.py''s notify_expiring_subscriptions() fires the reminder exactly once per subscription rather than on every cron run.';
