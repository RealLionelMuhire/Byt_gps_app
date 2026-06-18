-- Migration 009: Add lifecycle column to devices for inventory management
-- Tracks a device through 3 ownership states:
--   registered  → Admin added IMEI to DB. SIM inserted. Device has never connected.
--   in_stock    → Device has sent at least one TCP handshake (proven operational).
--                 Optionally verified by admin. Ready to sell.
--   sold        → Device paired to a customer (user_id IS NOT NULL).
--
-- Run with: psql $DATABASE_URL -f migrations/009_add_device_lifecycle.sql

ALTER TABLE devices
    ADD COLUMN IF NOT EXISTS lifecycle VARCHAR(20) NOT NULL DEFAULT 'registered';

COMMENT ON COLUMN devices.lifecycle IS
    'Inventory lifecycle state: registered (admin added, never connected) | in_stock (handshake received, ready to sell) | sold (paired to customer user_id)';

-- Also add sim_number column to store the SIM card number inside each device
ALTER TABLE devices
    ADD COLUMN IF NOT EXISTS sim_number VARCHAR(20) DEFAULT NULL;

COMMENT ON COLUMN devices.sim_number IS
    'Phone number of the SIM card inserted in the device. Used for SMS configuration commands.';

-- Backfill: any device that already has a user_id is 'sold'
UPDATE devices SET lifecycle = 'sold' WHERE user_id IS NOT NULL;

-- Backfill: any device with a last_connect but no user_id is 'in_stock'
UPDATE devices SET lifecycle = 'in_stock'
WHERE user_id IS NULL AND last_connect IS NOT NULL;

-- Index for inventory queries
CREATE INDEX IF NOT EXISTS idx_devices_lifecycle ON devices (lifecycle);
