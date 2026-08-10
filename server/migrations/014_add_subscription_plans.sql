-- Migration 014: Configurable subscription plans + device plan linking
-- Run with: psql $DATABASE_URL -f migrations/014_add_subscription_plans.sql

-- 1. Subscription schemes (admin-configurable)
CREATE TABLE IF NOT EXISTS subscription_plans (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    slug            VARCHAR(50) NOT NULL UNIQUE,
    billing_type    VARCHAR(20) NOT NULL DEFAULT 'recurrent',  -- one_time | recurrent
    price           FLOAT NOT NULL DEFAULT 0,
    currency        VARCHAR(10) NOT NULL DEFAULT 'RWF',
    duration_value  INTEGER NOT NULL DEFAULT 1,               -- e.g. 1
    duration_unit   VARCHAR(10) NOT NULL DEFAULT 'month',     -- day | week | month | year
    max_devices     INTEGER,                                   -- NULL = unlimited
    description     VARCHAR(500),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- 2. Link plans to devices
ALTER TABLE devices ADD COLUMN IF NOT EXISTS plan_id INTEGER REFERENCES subscription_plans(id);
CREATE INDEX IF NOT EXISTS idx_devices_plan_id ON devices (plan_id);

COMMENT ON COLUMN subscription_plans.billing_type IS
    'one_time = single payment (length = how long it lasts) | recurrent = recurring, price charged per length';
COMMENT ON COLUMN devices.plan_id IS
    'Subscription scheme linked to this device (admin-configured).';

-- 3. Seed the default plans (match the legacy hardcoded values in onboarding.py)
INSERT INTO subscription_plans (name, slug, billing_type, price, currency, duration_value, duration_unit, max_devices, description)
VALUES
    ('Trial',  'trial', 'one_time',  0,      'RWF', 14, 'day',   1,    'Free 14-day trial. 1 vehicle.'),
    ('Basic',  'basic', 'recurrent', 5000,   'RWF', 1,  'month', 3,    'Monthly plan. Up to 3 vehicles.'),
    ('Fleet',  'fleet', 'recurrent', 15000,  'RWF', 1,  'month', NULL, 'Monthly plan. Unlimited vehicles.')
ON CONFLICT (slug) DO NOTHING;
