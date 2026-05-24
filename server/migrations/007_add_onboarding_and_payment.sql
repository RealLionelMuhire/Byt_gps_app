-- Update users table with new onboarding fields
ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'owner';
ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_step INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_complete BOOLEAN NOT NULL DEFAULT FALSE;

-- If 'name' column exists from old schema, drop it or rename it
ALTER TABLE users DROP COLUMN IF EXISTS name;

-- Create vehicles table
CREATE TABLE IF NOT EXISTS vehicles (
    id SERIAL PRIMARY KEY,
    clerk_user_id VARCHAR(255) NOT NULL,
    device_id INTEGER REFERENCES devices(id),
    nickname VARCHAR(100) NOT NULL,
    plate VARCHAR(20) NOT NULL,
    make VARCHAR(100) NOT NULL,
    model VARCHAR(100) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_vehicles_clerk_user_id ON vehicles (clerk_user_id);
CREATE INDEX IF NOT EXISTS ix_vehicles_device_id ON vehicles (device_id);

-- Create subscriptions table
CREATE TABLE IF NOT EXISTS subscriptions (
    id SERIAL PRIMARY KEY,
    clerk_user_id VARCHAR(255) NOT NULL,
    plan_id VARCHAR(20) NOT NULL,
    status VARCHAR(20) DEFAULT 'active',
    started_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_subscriptions_clerk_user_id ON subscriptions (clerk_user_id);

-- Create payments table
CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    clerk_user_id VARCHAR(255) NOT NULL,
    tx_ref VARCHAR(255) NOT NULL UNIQUE,
    plan_id VARCHAR(20) NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    currency VARCHAR(10) DEFAULT 'RWF',
    status VARCHAR(20) NOT NULL,
    verified_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_payments_clerk_user_id ON payments (clerk_user_id);
CREATE INDEX IF NOT EXISTS ix_payments_tx_ref ON payments (tx_ref);
