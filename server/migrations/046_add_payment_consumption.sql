-- Migration 046: mark payments as consumed by the subscription they fund
--
-- Until now nothing recorded that a successful payment had already been
-- used to activate a subscription, so one payment could fund unlimited
-- subscriptions:
--   - POST /api/subscriptions accepted ANY past successful payment for the
--     plan — after a paid subscription expired, calling it again granted a
--     fresh period for free.
--   - POST /api/subscriptions/upgrade checked only that txRef was a
--     successful payment by the caller — not which plan it paid for, nor
--     whether it was already used — so a cheap plan's payment could buy an
--     expensive plan, repeatedly.
-- app/api/onboarding.py now atomically claims an unconsumed payment for the
-- exact target plan (see _claim_payment) and links it here.
--
-- BACKFILL: each existing successful payment is linked to the subscription
-- it activated — the earliest same-user, same-plan subscription created
-- from 5 minutes before to 1 day after the payment was verified, not
-- already claimed by another payment. A successful payment that never
-- activated anything stays unconsumed, so its owner can still use it.
--
-- Purely additive. IDEMPOTENT — safe to re-run (only touches rows with
-- consumed_at still NULL). Run with:
--   psql $DATABASE_URL -f migrations/046_add_payment_consumption.sql

BEGIN;

ALTER TABLE payments ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS subscription_id INTEGER REFERENCES subscriptions(id);

-- One payment funds at most one subscription, and vice versa.
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_subscription_id
    ON payments(subscription_id) WHERE subscription_id IS NOT NULL;

-- Pairs are only taken where the payment's earliest candidate subscription
-- is also that subscription's earliest candidate payment, so no two
-- payments can claim the same subscription. Any ambiguous leftover stays
-- unconsumed — the conservative side (its owner can still use it).
WITH candidates AS (
    SELECT p.id AS payment_id, s.id AS subscription_id, s.created_at,
           row_number() OVER (PARTITION BY p.id ORDER BY s.created_at) AS rank_for_payment,
           row_number() OVER (PARTITION BY s.id ORDER BY p.verified_at) AS rank_for_subscription
    FROM payments p
    JOIN subscriptions s
      ON s.clerk_user_id = p.clerk_user_id
     AND s.plan_id = p.plan_id
     AND s.created_at BETWEEN p.verified_at - interval '5 minutes'
                          AND p.verified_at + interval '1 day'
    WHERE p.status = 'successful'
      AND p.consumed_at IS NULL
      AND NOT EXISTS (SELECT 1 FROM payments o WHERE o.subscription_id = s.id)
)
UPDATE payments p
SET consumed_at = c.created_at,
    subscription_id = c.subscription_id
FROM candidates c
WHERE c.payment_id = p.id
  AND c.rank_for_payment = 1
  AND c.rank_for_subscription = 1;

COMMENT ON COLUMN payments.consumed_at IS 'When this payment was used to activate a subscription. NULL = not yet used. Set atomically by app/api/onboarding.py''s _claim_payment; a consumed payment can never activate another subscription.';
COMMENT ON COLUMN payments.subscription_id IS 'The subscription this payment activated.';

COMMIT;
