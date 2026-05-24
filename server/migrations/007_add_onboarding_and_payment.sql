-- Update users table with new onboarding and phone fields
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(30);
ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'owner';
ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_step INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_complete BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ALTER COLUMN email DROP NOT NULL;
CREATE INDEX IF NOT EXISTS ix_users_phone ON users (phone);

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
