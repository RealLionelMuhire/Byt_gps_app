-- Migration 008: Add pairing_pin to devices for secure device whitelisting
-- Run with: psql $DATABASE_URL -f migrations/008_add_pairing_pin.sql

ALTER TABLE devices
    ADD COLUMN IF NOT EXISTS pairing_pin VARCHAR(8) DEFAULT NULL;

COMMENT ON COLUMN devices.pairing_pin IS
    'Random 6-8 char secret code printed inside device packaging. Required during mobile app pairing to prevent IMEI hijacking.';
