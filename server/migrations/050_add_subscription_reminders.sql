-- Migration 050: log every expiry reminder that was sent
--
-- subscriptions.expiry_reminder_sent_at (migration 036) records one
-- "expiring soon" notice per subscription, and is set even when the email
-- was skipped (EmailJS not configured, no template, no address) — it means
-- "attempted", not "delivered". scripts/cron_expiry.py now sends staged
-- reminders (14, 7 and 3 days before expiry, plus the expiry notice) and
-- logs each one here with what actually happened on each channel, so the
-- admin dashboard can show who was reminded, when, and whether it went out.
--
-- expires_at is the subscription expiry the reminder was about: extending a
-- subscription makes the stages due again for the new date, while the
-- history of the old ones is kept.
--
-- email_sent / push_sent: TRUE = accepted by the provider, FALSE = not sent
-- (skipped or rejected), NULL = unknown (backfilled rows).
--
-- BACKFILL: every subscription with expiry_reminder_sent_at set gets a
-- '3d' row with unknown channel outcomes.
--
-- Purely additive. IDEMPOTENT — safe to re-run. Run with:
--   psql $DATABASE_URL -f migrations/050_add_subscription_reminders.sql

BEGIN;

CREATE TABLE IF NOT EXISTS subscription_reminders (
    id SERIAL PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    clerk_user_id VARCHAR(255) NOT NULL,
    stage VARCHAR(10) NOT NULL CHECK (stage IN ('14d', '7d', '3d', 'expired')),
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    sent_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    email_sent BOOLEAN,
    push_sent BOOLEAN,
    CONSTRAINT uq_subscription_reminders_stage UNIQUE (subscription_id, stage, expires_at)
);
CREATE INDEX IF NOT EXISTS idx_subscription_reminders_subscription ON subscription_reminders(subscription_id);
CREATE INDEX IF NOT EXISTS idx_subscription_reminders_clerk_user ON subscription_reminders(clerk_user_id);
ALTER TABLE subscription_reminders ENABLE ROW LEVEL SECURITY;

INSERT INTO subscription_reminders (subscription_id, clerk_user_id, stage, expires_at, sent_at, email_sent, push_sent)
SELECT s.id, s.clerk_user_id, '3d', s.expires_at, s.expiry_reminder_sent_at, NULL, NULL
FROM subscriptions s
WHERE s.expiry_reminder_sent_at IS NOT NULL
ON CONFLICT (subscription_id, stage, expires_at) DO NOTHING;

COMMENT ON TABLE subscription_reminders IS 'Every expiry reminder sent by scripts/cron_expiry.py, with per-channel outcome.';

COMMIT;
