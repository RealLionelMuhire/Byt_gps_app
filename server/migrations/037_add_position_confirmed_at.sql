-- Migration 037: Track when a device's confirmed live position last changed
-- Run with: psql $DATABASE_URL -f migrations/037_add_position_confirmed_at.sql

ALTER TABLE devices ADD COLUMN IF NOT EXISTS position_confirmed_at TIMESTAMP;

COMMENT ON COLUMN devices.position_confirmed_at IS
    'When last_latitude/last_longitude were last actually changed by a confirmed fix (see app/services/live_position.py) — distinct from last_update, which is bumped on every packet whether or not it was valid or changed the position.';
