-- Migration 048: per-vehicle subscriptions
--
-- Until now a subscription covered the whole account, and plans were
-- either "flat" or charged per paired device. Now every plan is priced per
-- vehicle and the customer chooses which vehicles to cover:
--
--   subscriptions.quantity     vehicle slots paid for
--   subscription_vehicles      which vehicles a subscription covers (at most
--                              `quantity`); uncovered vehicles keep working
--                              in the account but their premium features are
--                              refused (log-only first, see
--                              app/services/entitlements.py)
--   payments.purpose           "subscribe" | "add_vehicles"
--   payments.vehicle_ids       the vehicles a payment was made for, fixed at
--                              payment time so activation can't cover more
--
-- payments.subscription_id's unique index (migration 046) becomes unique
-- only for "subscribe" payments: several add_vehicles payments legitimately
-- point at the same subscription.
--
-- PRICING CHANGE: every plan becomes per vehicle (charge_scope =
-- 'per_device'). A plan that was "flat" keeps its price, which now means
-- per vehicle.
--
-- BACKFILL: every currently active subscription covers ALL of its owner's
-- vehicles, with one slot each — nobody loses anything they have today.
--
-- IDEMPOTENT — safe to re-run. Run with:
--   psql $DATABASE_URL -f migrations/048_add_per_vehicle_subscriptions.sql

BEGIN;

ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS quantity INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS subscription_vehicles (
    id SERIAL PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    payment_id INTEGER REFERENCES payments(id),
    added_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    CONSTRAINT uq_subscription_vehicles_pair UNIQUE (subscription_id, vehicle_id)
);
CREATE INDEX IF NOT EXISTS idx_subscription_vehicles_subscription ON subscription_vehicles(subscription_id);
CREATE INDEX IF NOT EXISTS idx_subscription_vehicles_vehicle ON subscription_vehicles(vehicle_id);
ALTER TABLE subscription_vehicles ENABLE ROW LEVEL SECURITY;

ALTER TABLE payments ADD COLUMN IF NOT EXISTS purpose VARCHAR(20) NOT NULL DEFAULT 'subscribe';
ALTER TABLE payments ADD COLUMN IF NOT EXISTS vehicle_ids JSON;

DROP INDEX IF EXISTS uq_payments_subscription_id;
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_subscribe_subscription
    ON payments(subscription_id) WHERE subscription_id IS NOT NULL AND purpose = 'subscribe';

UPDATE subscription_plans SET charge_scope = 'per_device' WHERE charge_scope <> 'per_device';
ALTER TABLE subscription_plans ALTER COLUMN charge_scope SET DEFAULT 'per_device';

INSERT INTO subscription_vehicles (subscription_id, vehicle_id)
SELECT s.id, v.id
FROM subscriptions s
JOIN vehicles v ON v.clerk_user_id = s.clerk_user_id
WHERE s.status = 'active' AND s.expires_at > (now() AT TIME ZONE 'utc')
ON CONFLICT (subscription_id, vehicle_id) DO NOTHING;

UPDATE subscriptions s
SET quantity = GREATEST(1, (SELECT count(*) FROM subscription_vehicles sv WHERE sv.subscription_id = s.id))
WHERE s.status = 'active' AND s.expires_at > (now() AT TIME ZONE 'utc');

COMMENT ON TABLE subscription_vehicles IS 'Vehicles a subscription covers (at most subscriptions.quantity). Every plan is priced per vehicle since migration 048.';
COMMENT ON COLUMN payments.vehicle_ids IS 'Vehicle ids the payment was made for, fixed at initiation; activation covers exactly these.';

COMMIT;
